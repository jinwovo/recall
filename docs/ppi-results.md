## Prediction-powered inference — measured

1,500 independent repetitions per row: draw a population, hand-label **50** items, let the judge score all **2,050**, publish a 95% interval, check whether it contained the truth. The estimand is the mean of Beta(5, 2), which is exactly 5/7 = 0.7143.

| judge behaviour | judge only | hand labels only | **PPI** |
|---|:---:|:---:|:---:|
| flattering, accurate | 0.0% | 94.0% | **94.3%** |
| flattering, noisy | 0.0% | 94.2% | **93.5%** |
| unbiased, noisy | 14.5% | 94.7% | **93.7%** |
| harsh, accurate | 0.0% | 94.4% | **94.4%** |
| uninformative | 0.0% | 94.8% | **94.3%** |

*Coverage: how often the published 95% interval actually contained the true value.* Averaging the judge is not a 95% interval — it is a narrow interval around whatever the judge believes, and when the judge is biased it is almost never right. PPI holds at the nominal rate regardless.

| judge behaviour | width, labels only | width, PPI | narrower by | λ | effective labels (from 50) |
|---|:---:|:---:|:---:|:---:|:---:|
| flattering, accurate | 0.0884 | 0.0291 | **67%** | 0.92 | **474** |
| flattering, noisy | 0.0882 | 0.0685 | **22%** | 0.46 | **84** |
| unbiased, noisy | 0.0881 | 0.0683 | **22%** | 0.43 | **85** |
| harsh, accurate | 0.0883 | 0.0293 | **67%** | 0.89 | **466** |
| uninformative | 0.0883 | 0.0879 | **1%** | 0.05 | **51** |

*The cost of a bad judge is zero, not negative:* the uninformative row drives λ to zero and lands back on the hand-label interval. Everything above it is precision bought without writing more labels.
