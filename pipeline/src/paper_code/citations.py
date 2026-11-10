from .search import logged_searches, run_search, search_workers, write_query_log
from ..utils import get_logger
from pipeline.src.utils import atomic_json

logger = get_logger(__name__)

def check_citations_of_top(top_results: list[dict], out: str, **search_kwargs) -> list[dict]:
    """
    Checks the citations of the top N papers and returns a list of paper records.

    Args:
        top_results: A list of the top N paper records from the initial search.
        out: The path to the output JSON file.

    Returns:
        A list of paper records for the papers found in the citation search.
    """
    citation_results = []
    seeds = [(idx, paper) for idx, paper in enumerate(top_results) if paper.get('cited_by_id')]
    for idx, paper in enumerate(top_results):
        if not paper.get('cited_by_id'):
            logger.info("[%d/%d] No cited_by_id for paper: %s", idx + 1, len(top_results), paper.get('title'))
    from pipeline.src.checkpoint import fingerprint
    per_seed = logged_searches("1.3_citations", [{"cites_id": p['cited_by_id']} for _, p in seeds],
                               lambda call: run_search(term=None, cites_id=call["cites_id"], **search_kwargs),
                               threads=search_workers(), desc="Citation searches", fp=fingerprint(search_kwargs))
    for (idx, paper), results in zip(seeds, per_seed):
        results = [dict(item) for item in results]  # two seeds with one cites_id share a result
        title, cited_by_id = paper.get('title'), paper.get('cited_by_id')
        for item in results:
            item['source'] = 'cites'
            # The citation edge: which seed this paper cites, and that seed's rank.
            item['seed'] = {'title': title, 'link': paper.get('link'), 'cited_by_id': cited_by_id,
                            'seed_rank': idx + 1, 'seed_citations': paper.get('citations')}
        logger.info("Citations found: %d for paper: %s", len(results), title)
        citation_results.extend(results)

    atomic_json(out, citation_results, indent=4)
    write_query_log(out)
    logger.info("Total citation results: %d", len(citation_results))

    return citation_results