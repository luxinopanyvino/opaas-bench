"""`sentinel`: the enforce vantage point (D36, D46, D50).

- `drift`: `opaas watch`, a scheduled one-shot check that a certificate still
  holds (D33, D36);
- `inventory`: the environment re-discovered on a schedule and compared with
  a baseline (N5);
- `proxy`: an observing reverse proxy in front of engines and tools, which
  records every call as a signed, hash-chained `AuditEvent` and evaluates
  structural policies in real time (D46);
- `gate`: the opt-in mode of the same proxy that allows, blocks, or holds an
  action for human approval (D50).
"""
