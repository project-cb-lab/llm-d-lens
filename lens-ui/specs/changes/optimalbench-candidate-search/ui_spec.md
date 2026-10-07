# UI specification

## Search Candidates stage

Add a shared OptimalBench configuration panel above the source families:

- Accelerator budget: integer, default 8.
- AIC system: text, default `b60`.
- AIC backend: vLLM, SGLang, or TRT-LLM.
- Results per mode: integer, default 10.

Predictive contains AIC Prediction. Search-based mirrors Compare Search:

- Optimized Baseline
- P/D Disaggregation
- E/P/D Disaggregation
- Tiered Prefix Cache

Candidate rows display source, topology, total accelerators, and prediction metrics when supplied by AIC. Search-space-only candidates display `Not predicted` rather than invented metrics. E/P/D rows include encode topology and a deployment limitation badge.

While searching, the existing loading button is used. Failures appear in an error panel and the prior candidate set remains available.
