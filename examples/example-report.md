# Citation and bibliography report

- Tool: latex-claim-and-bib-checker 0.1.0
- Date: 2026-09-25 23:18
- Input: paper.tex
- Bibliography: references.bib, software.bib
- Checks run: references, claims
- Mode: local
- Provider: local
- Model: qwen/qwen3.8-27b
- Metadata sources: crossref, openalex, datacite
- Abstract sources: arxiv, crossref, openalex, datacite, s2, core, europepmc
- Sources not used, key missing: springer (`SPRINGER_API_KEY`)
- Model requests: 10
- Tokens reported: input tokens 6482, output tokens 8093

> This report is a verification aid. Verdicts are based on abstracts only and must be checked by a person against the full text.

## Summary

| References | Count |
|---|---|
| Cited but undefined | 0 |
| Defined but uncited | 1 |
| MATCH | 7 |
| MISMATCH_MINOR | 2 |
| MISMATCH_MAJOR | 0 |
| NOT_FOUND_IN_ENABLED_SOURCES | 1 |
| SOURCE_ERROR | 0 |
| NOT_CHECKED | 0 |

| Claims | Count |
|---|---|
| Claims | 10 |
| SUSPICIOUS | 1 |
| PLAUSIBLE | 4 |
| SUPPORTED | 5 |
| lookup found | 10 |

## References

### Citation consistency

**Bibliography:** references.bib, software.bib

| Metric | Count |
|---|---|
| Cited keys | 9 |
| Defined keys | 10 |
| Cited but undefined (error) | 0 |
| Defined but uncited (warning) | 1 |

**Status: INCONSISTENT**

#### Defined but uncited

- `unused2020example`

### Metadata verification

**Sources:** crossref, openalex, datacite (stops at the first source with comparable fields)

| Status | Count | Meaning |
|---|---|---|
| MATCH | 7 | at least one field compared, no difference |
| MISMATCH_MINOR | 2 | formatting-level differences only (e.g. diacritics, year off by one) |
| MISMATCH_MAJOR | 0 | at least one substantive difference |
| NOT_FOUND_IN_ENABLED_SOURCES | 1 | every applicable enabled source answered without a matching record |
| SOURCE_ERROR | 0 | no record obtained and at least one source did not answer |
| NOT_CHECKED | 0 | no comparison possible (see note) |

#### Entries

| # | Key | DOI | Status | Origin | Compared with | Note |
|---|---|---|---|---|---|---|
| 1 | lecun2015deep | 10.1038/nature14539 | **MATCH** | API | [crossref (doi)](https://doi.org/10.1038/nature14539) | compared: title, author, year, journal, volume, issue, pages |
| 2 | hochreiter1997lstm | 10.1162/neco.1997.9.8.1735 | **MATCH** | API | [crossref (doi)](https://doi.org/10.1162/neco.1997.9.8.1735) | compared: title, author, year, journal, volume, issue, pages |
| 3 | he2016resnet | 10.1109/CVPR.2016.90 | **MATCH** | API | [crossref (doi)](https://doi.org/10.1109/cvpr.2016.90) | compared: title, author, year, journal, pages |
| 4 | vaswani2017attention | 10.48550/arXiv.1706.03762 | **MATCH** | API | [datacite (doi)](https://doi.org/10.48550/arxiv.1706.03762) | compared: title, author, year |
| 5 | kingma2015adam | — | **MISMATCH_MINOR** | API | [datacite (arxiv_id)](https://doi.org/10.48550/arxiv.1412.6980) | differs: year \[datacite\] |
| 6 | breiman2001random | 10.1023/A:1010933404324 | **MISMATCH_MINOR** | API | [crossref (doi)](https://doi.org/10.1023/a:1010933404324) | differs: year \[crossref\] |
| 7 | gu2020empirical | 10.1093/rfs/hhaa009 | **MATCH** | API | [crossref (doi)](https://doi.org/10.1093/rfs/hhaa009) | compared: title, author, year, journal, volume, issue, pages |
| 8 | harris2020numpy | 10.1038/s41586-020-2649-2 | **MATCH** | API | [crossref (doi)](https://doi.org/10.1038/s41586-020-2649-2) | compared: title, author, year, journal, volume, issue, pages |
| 9 | pedregosa2011scikit | — | **MATCH** | API | [openalex (title)](https://doi.org/10.5555/1953048.2078195) | compared: title, author, year, journal |
| 10 | unused2020example | — | **NOT_FOUND_IN_ENABLED_SOURCES** | API | — | openalex: no title match |

#### Differences

**kingma2015adam** (MISMATCH_MINOR)

- year [datacite, minor]: bib `2015` vs source `2014`

**breiman2001random** (MISMATCH_MINOR)

- year [crossref, minor]: bib `2000` vs source `2001`

#### Lookup details

- **unused2020example**: openalex: ABSENT (no title match)

## Claims

### SUSPICIOUS

_The abstract appears to contradict or not to support the sentence, review first._

#### 10. he2016resnet

> Residual connections were first introduced to forecast short-term interest rates from central bank announcements \\citep{he2016resnet}.

- Title: Deep Residual Learning for Image Recognition
- Verdict: **SUSPICIOUS**
- Lookup: found
- Evidence: abstract
- Evidence source: openalex (origin API, matched by doi)
- Link: https://doi.org/10.1109/cvpr.2016.90
- Rationale: The abstract describes residual learning for image recognition, ImageNet, and COCO tasks. It does not mention forecasting short-term interest rates or central bank announcements, so the cited work does not support the claim.

### PLAUSIBLE

_Consistent with the abstract but not stated there, spot-check._

#### 1. lecun2015deep

> Deep neural networks \\citep{lecun2015deep} build on earlier work on recurrent architectures \\citep{hochreiter1997lstm} and residual connections \\citep{he2016resnet}.

- Title: Deep learning
- Verdict: **PLAUSIBLE**
- Lookup: found
- Evidence: abstract
- Evidence source: europepmc (origin API, matched by doi)
- Link: https://doi.org/10.1038/nature14539
- Rationale: The abstract discusses deep learning, multiple processing layers, and recurrent nets, so it is broadly consistent with the citing sentence. However, it does not state that deep neural networks build on earlier recurrent architectures or residual connections, nor does it mention residual connections at all.

#### 5. kingma2015adam

> Attention-only models \\citet{vaswani2017attention} are usually trained with adaptive optimisers \\citep{kingma2015adam}.

- Title: Adam: A Method for Stochastic Optimization
- Verdict: **PLAUSIBLE**
- Lookup: found
- Evidence: abstract
- Evidence source: arxiv (origin API, matched by arxiv_id)
- Link: https://arxiv.org/abs/1412.6980
- Evidence quote: "We introduce Adam, an algorithm for first-order gradient-based optimization of stochastic objective functions, based on adaptive estimates of lower-order moments."
- Rationale: The abstract shows that Adam is an optimization method based on adaptive estimates, consistent with its use as an example of an adaptive optimizer. However, it does not mention attention-only models or state that such models are usually trained with Adam, so the specific claim is not directly supported.

#### 7. gu2020empirical

> Tree ensembles \\citep{breiman2001random} remain strong baselines in empirical asset pricing \\citep\[see\]\[\]{gu2020empirical}.

- Title: Empirical Asset Pricing via Machine Learning
- Verdict: **PLAUSIBLE**
- Lookup: found
- Evidence: abstract
- Evidence source: crossref (origin API, matched by doi)
- Link: https://doi.org/10.1093/rfs/hhaa009
- Evidence quote: "We identify the best-performing methods (trees and neural networks) and trace their predictive gains to allowing nonlinear predictor interactions missed by other methods."
- Rationale: The abstract shows trees are among best-performing methods for empirical asset pricing, consistent with tree ensembles being strong. However, it does not explicitly state that tree ensembles remain strong baselines, so the specific baseline claim is not directly supported.

#### 9. pedregosa2011scikit

> All computations use standard scientific Python libraries \\citep{harris2020numpy, pedregosa2011scikit}.

- Title: Scikit-learn: Machine Learning in Python
- Verdict: **PLAUSIBLE**
- Lookup: found
- Evidence: abstract
- Evidence source: openalex (origin API, matched by title)
- Link: https://doi.org/10.5555/1953048.2078195
- Evidence quote: "Scikit-learn is a Python module integrating a wide range of state-of-the-art machine learning algorithms for medium-scale supervised and unsupervised problems."
- Rationale: The abstract confirms that scikit-learn is a Python module for machine learning, consistent with citing it as a scientific Python library. However, it does not explicitly state that it is a standard scientific Python library or that it was used for the computations described in the citing sentence.

### SUPPORTED

_The quoted passage of the abstract supports the sentence._

#### 2. hochreiter1997lstm

> Deep neural networks \\citep{lecun2015deep} build on earlier work on recurrent architectures \\citep{hochreiter1997lstm} and residual connections \\citep{he2016resnet}.

- Title: Long Short-Term Memory
- Verdict: **SUPPORTED**
- Lookup: found
- Evidence: abstract
- Evidence source: crossref (origin API, matched by doi)
- Link: https://doi.org/10.1162/neco.1997.9.8.1735
- Evidence quote: "Learning to store information over extended time intervals by recurrent backpropagation takes a very long time, mostly because of insufficient, decaying error backflow."
- Rationale: The abstract explicitly discusses recurrent backpropagation and introduces LSTM as a recurrent network method, supporting the citing sentence’s characterization of the work as earlier work on recurrent architectures.

#### 3. he2016resnet

> Deep neural networks \\citep{lecun2015deep} build on earlier work on recurrent architectures \\citep{hochreiter1997lstm} and residual connections \\citep{he2016resnet}.

- Title: Deep Residual Learning for Image Recognition
- Verdict: **SUPPORTED**
- Lookup: found
- Evidence: abstract
- Evidence source: openalex (origin API, matched by doi)
- Link: https://doi.org/10.1109/cvpr.2016.90
- Evidence quote: "We present a residual learning framework to ease the training of networks that are substantially deeper than those used previously."
- Rationale: The abstract explicitly presents a residual learning framework for substantially deeper networks and reports that residual networks gain accuracy from increased depth, directly supporting the claim that deep networks build on residual connections.

#### 4. vaswani2017attention

> Attention-only models \\citet{vaswani2017attention} are usually trained with adaptive optimisers \\citep{kingma2015adam}.

- Title: Attention Is All You Need
- Verdict: **SUPPORTED**
- Lookup: found
- Evidence: abstract
- Evidence source: datacite (origin API, matched by doi)
- Link: https://doi.org/10.48550/arxiv.1706.03762
- Evidence quote: "We propose a new simple network architecture, the Transformer, based solely on attention mechanisms, dispensing with recurrence and convolutions entirely."
- Rationale: The cited work explicitly introduces an attention-only architecture, the Transformer, which directly supports the sentence’s reference to attention-only models. The abstract does not discuss adaptive optimizers, but that part is separately cited to Kingma and Ba, so the cited work supports the attributed claim.

#### 6. breiman2001random

> Tree ensembles \\citep{breiman2001random} remain strong baselines in empirical asset pricing \\citep\[see\]\[\]{gu2020empirical}.

- Title: Random Forests
- Verdict: **SUPPORTED**
- Lookup: found
- Evidence: abstract
- Evidence source: s2 (origin API, matched by doi)
- Link: https://doi.org/10.1023/a:1010933404324
- Evidence quote: "Random forests are a combination of tree predictors such that each tree depends on the values of a random vector sampled independently and with the same distribution for all trees in the forest."
- Rationale: The abstract describes random forests as ensembles of tree predictors, which matches the sentence's citation for tree ensembles. It does not address empirical asset pricing, but that part is cited to gu2020empirical. Thus the cited work supports the methodological reference, not the asset-pricing baseline claim.

#### 8. harris2020numpy

> All computations use standard scientific Python libraries \\citep{harris2020numpy, pedregosa2011scikit}.

- Title: Array programming with NumPy
- Verdict: **SUPPORTED**
- Lookup: found
- Evidence: abstract
- Evidence source: crossref (origin API, matched by doi)
- Link: https://doi.org/10.1038/s41586-020-2649-2
- Evidence quote: "NumPy is the primary array programming library for the Python language."
- Rationale: The abstract explicitly identifies NumPy as the primary array programming library for Python and as foundational to the scientific Python ecosystem. This supports the citing sentence’s characterization of NumPy as a standard scientific Python library used for computations.
