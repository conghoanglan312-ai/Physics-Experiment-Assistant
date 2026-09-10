from google import genai
from queue import Queue
from threading import Thread
import os 
from dotenv import load_dotenv
import time

from llm.logs.logger import LLMlogger
from .base import BaseLLM
from llm.util.generateModelMessage import *
from llm.config.settings import MODELS

config = MODELS["gemini"]
load_dotenv()

class GeminiLLM(BaseLLM):
    def __init__ (self):
        self.client = genai.Client(
            api_key = os.getenv("GEMINI_API_KEY")
        )
        self.log = LLMlogger()

    def stream(
            self,
            userQuery: str,
            previousID,
            system_prompt: str = None,
            history = None
        ):

        # Log Initialization
        self.log.start(config["model"])
        start_time = time.time()

        try: 
            interaction = self.client.interactions.create(
                model=config["model"],
                input=userQuery,
                previous_interaction_id=previousID,
                system_instruction=system_prompt,
                stream=config["stream"],
            )
        except Exception as e:
            message = generateErrorMessage(e)
            self.log.error(config["model"], 0, e)
            yield message
            return

        # Log User Query
        self.log.query(config["model"], userQuery)

        queue = Queue()

        def consume_stream():
            token_count = -1 # Indicate Error
            try:
                for event in interaction:
                    # Get ID
                    if event.event_type == "interaction.created":
                        message = generateIDMessage(event.interaction.id)

                        queue.put(message)

                    # Get Text Response
                    elif event.event_type == "step.delta":
                        if hasattr(event.delta, "text"):
                            text = event.delta.text
                            message = generateTextMessage(text)

                            queue.put(message)

                    # Get Token Usage
                    if event.event_type == "interaction.completed":
                        if hasattr(event.interaction, "usage"):
                            token_count = event.interaction.usage.total_tokens
                        
                # After Exhausted of interaction
                message = generateDoneMessage()
                queue.put(message)

                # Finish Logging
                latency = time.time() - start_time
                self.log.finish(config["model"], latency, token_count)

            except Exception as e:
                # Any kind of Error Occured
                message = generateErrorMessage(e)
                queue.put(message)

                # Log Error Occured
                latency = time.time() - start_time
                self.log.error(config["model"], latency, e)

        Thread(target = consume_stream, daemon=True).start()

        while True:
            event = queue.get()

            yield event

            if event.type == "done" or event.type == "error": 
                break