from .timeout import StreamTimeout
from llm.AI_model.base import BaseLLM, LLMEvent

class TimeoutLLM(BaseLLM):
    def __init__(self, model):
        self.model = model

    def stream(
        self,
        userQuery, 
        previousID = None,
        system_prompt = None, 
        history = None
    ):
        timeout = StreamTimeout()

        for event in self.model.stream(
            userQuery=userQuery, 
            system_prompt=system_prompt, 
            previousID=previousID, 
            history=history
        ):
            if timeout.isTimeout():
                yield LLMEvent(
                    type="error",
                    data="Timeout"
                )

                return

            timeout.update()

            yield event
        