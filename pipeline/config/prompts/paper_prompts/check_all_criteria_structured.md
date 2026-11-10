You are screening a research paper's full text against inclusion and exclusion criteria.

For EACH numbered criterion below, decide whether the paper's full text genuinely satisfies it. A criterion that lists terms joined by "OR" is satisfied if ANY one of its terms applies. Judge the paper's actual subject matter and methods — a passing mention or a background citation does NOT satisfy a criterion.

Required criteria (the paper is relevant only if it satisfies ALL of these):
{pos_block}

Excluded criteria (the paper must satisfy NONE of these):
{neg_block}

Respond with ONLY a JSON object of exactly this shape — no prose, no markdown, no code fences:
{"positives": {"0": true, "1": false}, "negatives": {"0": false, "1": false, "2": false}}

Each key is the criterion number shown above; each value is a boolean. Include every criterion number. If a required criterion is not clearly satisfied by the text, its value must be false.
