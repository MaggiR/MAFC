import dataclasses
import re
from typing import Sequence

from common import utils
from common.console import orange
from common.label import Label
from common.modeling import Model
from eval.logger import EvaluationLogger
from safe.config import debug_safe, max_steps, max_retries
from safe.prompts.prompt import ReasonPrompt
from common.results import SearchResult
from safe.searcher import extract_knowledge
from common.document import FCDocument
from safe.prompts.prompt import JudgePrompt
from common.utils import is_guardrail_hit


@dataclasses.dataclass()
class FinalAnswer:
    response: str
    answer: str


class Judge:
    """Determines the truthfulness of a claim given a collection of evidence."""

    def __init__(self, model: Model, logger: EvaluationLogger, classes: list[Label]):
        self.model = model
        self.classes = classes
        self.debug = debug_safe
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.latest_reasoning = None

        self.logger = logger

    def judge(self, doc: FCDocument) -> Label:
        judge_prompt = JudgePrompt(doc, self.classes)
        n_retries = 0
        while (verdict := self._generate_verdict(str(judge_prompt))) == Label.REFUSED_TO_ANSWER:
            n_retries += 1
            if n_retries > self.max_retries:
                break
        return verdict

    def _generate_verdict(self, prompt: str) -> Label:
        # Generate an answer
        response = self.model.generate(prompt, do_debug=self.debug)
        self.latest_reasoning = response

        # Validate model response
        if is_guardrail_hit(response):
            self.logger.log(utils.GUARDRAIL_WARNING)
            self.logger.log(orange("PROMPT:\n" + prompt))
            return Label.REFUSED_TO_ANSWER

        # Extract the verdict
        return self._extract_verdict(response)

    def _extract_verdict(self, response: str) -> Label:
        """Extract label from response"""
        answer = utils.extract_last_code_span(response)
        answer = re.sub(r'[^\w\s]', '', answer).strip().lower()

        if not answer:
            # No valid response given, therefore returning refused label
            return Label.REFUSED_TO_ANSWER

        try:
            verdict = Label(answer)
        except ValueError:
            # Maybe the label is a substring of the answer
            for c in self.classes:
                if c.value in answer:
                    return c

            verdict = Label.REFUSED_TO_ANSWER

        return verdict

    def get_latest_reasoning(self) -> str:
        return self.latest_reasoning

    def reason(self,
               claim: str,
               evidence: list[SearchResult]
    ) -> (Label, str):
        """Takes the claim and the gathered evidence, determines the
        claim's veracity through reasoning and returns the verdict with
        the reasoning as justification."""
        final_answer, num_tries = None, 0
        while not final_answer and num_tries <= self.max_retries:
            num_tries += 1
            final_answer = self.maybe_get_final_answer(claim, evidence=evidence)

        if final_answer is None:
            self.logger.log(orange("Unable to parse reasoning answer."))
            return Label.REFUTED, ""

        predicted_label = Label(final_answer.answer.lower())
        return predicted_label, final_answer.response

    def maybe_get_final_answer(self,
                               claim: str,
                               evidence: list[SearchResult],
                               ) -> FinalAnswer | None:
        """Get the final answer from the model."""
        # Construct the reasoning prompt
        knowledge = extract_knowledge(evidence)
        reason_prompt = ReasonPrompt(claim, knowledge, self.classes)

        model_response = self.model.generate(str(reason_prompt), do_debug=self.debug)

        # Validate model response
        if is_guardrail_hit(model_response):
            self.logger.log(utils.GUARDRAIL_WARNING)
            self.logger.log(orange(f"Reason prompt with claim {claim} and knowledge {knowledge}"))
            answer = 'refused'
            return FinalAnswer(response=model_response, answer=answer)
        answer = utils.extract_first_square_brackets(model_response)
        answer = re.sub(r'[^\w\s]', '', answer).strip()

        valid_labels = [label.value.lower() for label in Label]

        if model_response and answer.lower() in valid_labels:
            return FinalAnswer(response=model_response, answer=answer)

        else:
            # Adjust the model response
            select = f"Respond with one word! From {valid_labels}, select the most fitting for the following string:\n"
            adjusted_response = self.model.generate(select + model_response).lower()
            self.logger.log(orange(f"No answer label was found - likely due to wrong formatting."
                                   f"\nModel Output: {model_response}"
                                   f"\nAdjusted Output: {adjusted_response}"))

            if adjusted_response not in valid_labels:
                self.logger.log(orange(f"Error in generating answer. Defaulting to '{Label.REFUSED_TO_ANSWER}'\n"))
                adjusted_response = Label.REFUSED_TO_ANSWER.value

            return FinalAnswer(response=model_response, answer=adjusted_response)
