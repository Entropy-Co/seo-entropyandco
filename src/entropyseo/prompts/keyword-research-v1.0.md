# Entropy & Co. Keyword Clusterer v1.0

Group the supplied keyword candidates into 3 to 8 topical clusters for an AI automation and AI agents agency blog.
Return structured fields only.

- Each cluster has a `hub_term` copied exactly from the candidates (the broadest, highest-scoring term), `spokes`
  copied exactly from the candidates, the best-matching `pillar` from the supplied pillar list, and a
  `content_type_hint`.
- `strategic_headline`: one sentence on where the strongest opportunity is.
- `gap_notes`: up to 5 short notes on reader questions the candidates suggest but no candidate covers directly.

Do not invent keywords, volumes, or difficulty scores. Only use terms from the candidate list.
