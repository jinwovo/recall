Coverage promised: 90%. 200 independent streams per scenario, 3000 queries each, change at query 1000.

| scenario | fixed | window | adaptive | level offset | compensating |
|---|---|---|---|---|---|
| location shift (+0.30) | 0.459 | 0.876 | 0.900 | 0.027 | 0% |
| score saturation | 0.306 | 0.939 | 0.903 | 0.514 | 100% |

location shift (+0.30): recovery under 100 queries (100% of streams); deterministic bound held in 100%; worst single fixed-threshold stream 0.396
score saturation: recovery under 100 queries (100% of streams); deterministic bound held in 100%; worst single fixed-threshold stream 0.284