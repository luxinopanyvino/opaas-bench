"""The evidence core shared by every module (D41).

What makes a result trustworthy, independently of what was measured:
canonical serialization (`canonical`), hashes (`hashing`), signatures
(`credential`), the issuance log (`transparency`), statistics (`stats`), the
report schema (`report`), the deployment fingerprint (`fingerprint`), and for
agents the `AuditEvent` schema (`events`), structural policies (`policy`),
the auditability metrics (`metrics`), log integrity (`integrity`) and the
evidence-document envelope (`evidence`).

Nothing here imports from a module above it (`bench`, `discover`, `smith`,
`swarm`, `sentinel`, `inquest`, `card`).
"""
