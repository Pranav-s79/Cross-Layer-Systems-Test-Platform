# textlog example (synthetic)

**Every log in `logs/` is SYNTHETIC**: hand-written for this example, not
captured from any real system. The formats are read by the built-in profiles
`synthetic_client` and `synthetic_server`
(`crosslayer/integrations/textlog/profiles/`).

| File | What it shows |
|---|---|
| `logs/client.log` | Client log: ISO 8601 UTC timestamps; its clock is the reference |
| `logs/server.log` | Server log: local time (UTC+01:00), no year, clock 250 ms ahead |
| `logs/*_slow_reply.log` | Variant: request 1003 takes 180 ms |
| `contracts.yaml` | Three contracts; two join client and server events by request id |
| `baseline.yaml` | Must PASS |
| `slow_reply.yaml` | Must FAIL `demo.request_answered` |

```sh
crosslayer suite experiments/textlog/suite.yaml --out runs
crosslayer test-contracts experiments/textlog/contract_tests.yaml --require-coverage
```

See [docs/log-analysis.md](../../docs/log-analysis.md).
