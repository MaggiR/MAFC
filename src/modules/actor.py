from src.common.action import Action
from src.common.document import FCDocument
from src.common.modeling import LLM
from src.common.results import Evidence
from src.eval.logger import EvaluationLogger
from src.modules.result_summarizer import ResultSummarizer
from src.tools.tool import Tool


class Actor:
    """Agent that executes given Actions and returns the resulted Evidence."""

    def __init__(self, tools: list[Tool], llm: LLM, logger: EvaluationLogger):
        self.tools = tools
        self.result_summarizer = ResultSummarizer(llm, logger)

    def perform(self, actions: list[Action], doc: FCDocument) -> list[Evidence]:
        # TODO: Parallelize
        all_evidence = []
        for action in actions:
            all_evidence.append(self._perform_single(action, doc))
        return all_evidence

    def _perform_single(self, action: Action, doc: FCDocument) -> Evidence:
        tool = self.get_corresponding_tool_for_action(action)
        results = tool.perform(action)
        results = self.result_summarizer.summarize(results, doc)
        summary = ""  # TODO: Summarize result summaries
        return Evidence(summary, list(results))

    def get_corresponding_tool_for_action(self, action: Action) -> Tool:
        for tool in self.tools:
            if type(action) in tool.actions:
                return tool
        raise ValueError(f"No corresponding tool available for Action '{action}'.")

    def reset(self):
        """Resets all tools (if applicable)."""
        for tool in self.tools:
            tool.reset()
