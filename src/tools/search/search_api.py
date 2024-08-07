from abc import ABC

from src.common.results import SearchResult
from src.eval.logger import EvaluationLogger
from src.utils.console import yellow


class SearchAPI(ABC):
    """Abstract base class for all local and remote search APIs."""
    name: str
    is_free: bool
    is_local: bool

    def __init__(self, logger: EvaluationLogger = None):
        self.logger = logger
        self.total_searches = 0

    def _before_search(self, query: str):
        self.total_searches += 1
        if self.logger is not None:
            self.logger.log(yellow(f"Searching {self.name} with query: {query}"))

    def search(self, query: str, limit: int) -> list[SearchResult]:
        """Runs the API by submitting the query and obtaining a list of search results."""
        self._before_search(query)
        return self._call_api(query, limit)

    def _call_api(self, query: str, limit: int) -> list[SearchResult]:
        raise NotImplementedError()
