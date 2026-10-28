"""What gets retried, and what must not.

MAX_ATTEMPTS sat in config for the whole project while nothing retried
anything. The tests that matter here are the refusals: a gate failure, a
missing credential and an unrecognised error must never be retried.
"""

from __future__ import annotations

import pytest

from pipeline import recovery
from pipeline.errors import ConfigError, GateError, PipelineError, ScrubError, StageError

# ---- what is worth retrying -------------------------------------------


@pytest.mark.parametrize("message", [
    "upload failed: [Errno 54] Connection reset by peer",
    "cannot reach the Instagram API: timed out",
    "Instagram API returned 503. service not available",
    "Instagram API returned 502. bad gateway",
    "Instagram API returned 429. too many requests",
    "temporary failure in name resolution",
    "remote end closed connection without response",
])
def test_a_network_failure_is_retried(message):
    assert recovery.classify(StageError(message)).retry_now


@pytest.mark.parametrize("message", [
    "YouTube daily quota exhausted. Retry tomorrow",
    "quotaExceeded",
    "dailyLimitExceeded for this project",
])
def test_a_spent_quota_waits_rather_than_retrying(message):
    """Retrying in thirty seconds is pointless; this needs tomorrow."""
    verdict = recovery.classify(StageError(message))
    assert verdict.kind == "later"
    assert not verdict.retry_now


# ---- what must never be retried ---------------------------------------


def test_a_gate_failure_is_never_retried():
    """Retrying one would be an attempt to route around the gate."""
    verdict = recovery.classify(GateError("episode 002 has no approved_at"))
    assert verdict.kind == "needs_human"
    assert "decision" in verdict.why


def test_a_gate_failure_is_not_retried_even_if_it_reads_as_a_timeout():
    """Classification goes by type first, not by how the message reads,
    so wording can never turn a gate into a transient fault."""
    verdict = recovery.classify(GateError("connection timed out at the gate"))
    assert not verdict.retry_now


def test_a_draft_voice_is_not_retried():
    assert not recovery.classify(
        GateError("narrated with the draft voice engine")).retry_now


def test_a_scrub_finding_is_not_retried():
    """A secret in the data needs removing, not attempting again."""
    assert not recovery.classify(
        ScrubError("AWS key found in input/data.json")).retry_now


def test_missing_configuration_is_not_retried():
    """Credentials cannot be retried into existence."""
    verdict = recovery.classify(
        ConfigError("no YouTube credentials at secrets/youtube_token.json"))
    assert verdict.kind == "needs_human"


def test_a_missing_consent_file_is_not_retried():
    assert not recovery.classify(
        StageError("voices/x has no consent.md")).retry_now


def test_an_unrecognised_failure_is_not_retried():
    """A system that retries whatever it does not understand will
    eventually do something unrecoverable sixteen times in a row."""
    verdict = recovery.classify(StageError("something nobody has seen"))
    assert verdict.kind == "needs_human"
    assert "unrecognised" in verdict.why


def test_a_sync_drift_is_not_retried():
    """Retrying would hide a real bug behind an intermittent one."""
    assert not recovery.classify(
        StageError("rejected a 120ms drift between audio and video")).retry_now


# ---- backoff ----------------------------------------------------------


def test_backoff_grows():
    plain = [recovery.backoff(a) for a in (1, 2, 3)]
    assert plain == sorted(plain) and plain[0] < plain[-1]


def test_backoff_is_capped_even_after_jitter():
    """Jittering a capped value pushed it back over the cap: a stated
    120s maximum returned 140s."""
    for attempt in range(1, 12):
        for seed in ("upload", "instagram", "export"):
            assert recovery.backoff(attempt, seed) <= recovery.BACKOFF_MAX_SEC


def test_backoff_is_jittered_so_retries_do_not_lock_step():
    assert recovery.backoff(2, "a") != recovery.backoff(2, "b")


def test_backoff_is_reproducible_for_one_seed():
    assert recovery.backoff(3, "upload") == recovery.backoff(3, "upload")


# ---- the retry loop ---------------------------------------------------


def test_a_transient_failure_is_retried_until_it_works():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise StageError("connection reset by peer")
        return "done"

    got = recovery.with_retry(flaky, what="t", attempts=5, sleep=lambda s: None)
    assert got == "done"
    assert len(attempts) == 3


def test_a_permanent_failure_raises_on_the_first_attempt():
    attempts = []

    def broken():
        attempts.append(1)
        raise GateError("no approved_at")

    with pytest.raises(GateError):
        recovery.with_retry(broken, what="t", attempts=5, sleep=lambda s: None)
    assert len(attempts) == 1, "a gate failure must not be attempted twice"


def test_the_real_error_survives_running_out_of_attempts():
    """The ledger and the operator need the cause, not a wrapper."""
    def always():
        raise StageError("connection reset by peer")

    with pytest.raises(StageError, match="connection reset"):
        recovery.with_retry(always, what="t", attempts=2, sleep=lambda s: None)


def test_it_waits_between_attempts():
    waits = []

    def always():
        raise StageError("timed out")

    with pytest.raises(StageError):
        recovery.with_retry(always, what="t", attempts=3,
                            sleep=waits.append)
    assert len(waits) == 2, "waits between attempts, not after the last"
    assert all(w > 0 for w in waits)


def test_a_single_attempt_still_runs_the_work():
    assert recovery.with_retry(lambda: 7, what="t", attempts=1) == 7


def test_a_non_pipeline_error_is_not_swallowed():
    """An unexpected exception should surface, not be retried."""
    def boom():
        raise ValueError("a bug, not a failure mode")

    with pytest.raises(ValueError):
        recovery.with_retry(boom, what="t", sleep=lambda s: None)


# ---- which stages ------------------------------------------------------


def test_only_network_stages_are_retried():
    """A render is deterministic: a retry fails identically and costs a
    minute of compute."""
    for stage in ("upload", "instagram", "export", "script"):
        assert recovery.worth_retrying(stage)
    for stage in ("assemble", "charts", "voice", "thumbnail", "shorts"):
        assert not recovery.worth_retrying(stage)


def test_the_verdict_reads_as_a_sentence():
    assert "needs you" in recovery.describe(GateError("no approved_at"))
    assert "retrying" in recovery.describe(StageError("timed out"))


def test_the_plan_is_serialisable():
    plan = recovery.plan(StageError("timed out"))
    assert plan["kind"] == "transient" and plan["retry_now"] is True
    assert plan["max_attempts"] >= 1


def test_a_long_message_is_shortened_for_a_table():
    assert len(recovery.short(PipelineError("x " * 200), limit=40)) == 40
