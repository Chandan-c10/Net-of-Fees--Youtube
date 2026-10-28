"""Pipeline stages.

Each reads the manifest, does one job, writes its result back, and is safe
to re-run. A stage never infers state from the filesystem and never emits
a degraded artifact to keep going.
"""
