# The default dimension mapping

Dimensions are aliases for metric ids, published and versioned in the store. Inspect mapping.json
and the running compute service's GET /coverage together before declaring a run runnable.

Toxicity requires a declared embedder and group prototypes; the compute image includes its
clustering extra without torch. Privacy is refused because the detector benchmark requires ground
truth absent from a Roast Dataset. Empty mapping entries are refused at the gate. Regulatory is
registered but requires an explicitly chosen mapping or metric selection, the run's own KB,
embedder and reranker, and a reranker declared to answer the support question
(`params.reranker_score_meaning: supports`, ADR 0019); a run whose reranker is not so declared is
refused at the gate. A registry entry alone does not make a dimension covered.

Robustness, security and accountability map to metrics of the same id that are declared ahead of
the gaussia release that ships them (ADR 0020). They are registered, so the mapping can name them
and GET /coverage reports them with `awaiting` set and `installed: false`, and a run that declares
one of the three is refused at the gate naming the release, never with "no metric covering". When
gaussia ships a module, the metric's builder is written under the same id: the id is Yield's, as
`context` is, so the mapping does not change; a metric that arrives under a different id is a new
mapping version, never an edit of this one.
