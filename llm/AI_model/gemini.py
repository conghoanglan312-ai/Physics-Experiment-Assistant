from google import genai
from queue import Queue
from threading import Thread
import os 
from dotenv import load_dotenv
import time

from llm.logs.logger import LLMlogger
from .base import BaseLLM, LLMEvent
load_dotenv()

class GeminiLLM(BaseLLM):
    def __init__ (self):
        self.client = genai.Client(
            api_key = os.getenv("GEMINI_API_KEY")
        )
        self.model_name = "gemini-3.1-flash-lite"
        self.log = LLMlogger()

    def stream(
            self,
            userQuery: str,
            previousID,
            system_prompt: str = None,
            history = None
        ):

        # Log Initialization
        self.log.start(self.model_name)
        start_time = time.time()

        try: 
            interaction = self.client.interactions.create(
                model=self.model_name,
                input=userQuery,
                previous_interaction_id=previousID,
                system_instruction=system_prompt,
                stream=True
            )
        except Exception as e:
            yield LLMEvent(
                type="error",
                data=str(e)
            )
            self.log.error(self.model_name, 0, e)
            return

        # Log User Query
        self.log.query(self.model_name, userQuery)

        queue = Queue()

        def consume_stream():
            token_count = -1 # Indicate Error
            try:
                # TODO: Add timeout feature (30s for initial, 10s for chunks)
                for event in interaction:
                    # Get ID
                    if event.event_type == "interaction.created":
                        queue.put(LLMEvent(type = "id", data = event.interaction.id))

                    # Get Text Response
                    elif event.event_type == "step.delta":
                        if hasattr(event.delta, "text"):
                            text = event.delta.text

                            queue.put(LLMEvent(type = "text", data = text))

                    # Get Token Usage
                    if event.event_type == "interaction.completed":
                        if hasattr(event.interaction, "usage"):
                            token_count = event.interaction.usage.total_tokens
                        
                # After Exhausted of interaction
                queue.put(LLMEvent(type = "done"))

                # Finish Logging
                latency = time.time() - start_time
                self.log.finish(self.model_name, latency, token_count)

            except Exception as e:
                # Any kind of Error Occured
                queue.put(LLMEvent(type = "error", data = str(e)))

                # Log Error Occured
                latency = time.time() - start_time
                self.log.error(self.model_name, latency, e)

        Thread(target = consume_stream, daemon=True).start()

        while True:
            event = queue.get()

            yield event

            if event.type == "done" or event.type == "error": 
                break