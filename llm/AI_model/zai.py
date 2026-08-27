# ZhiPu AI (China)

import os
from dotenv import load_dotenv
from openai import OpenAI
import time
from queue import Queue
from threading import Thread
load_dotenv()

from llm.logs.logger import LLMlogger
from .base import BaseLLM, LLMEvent

class ZaiLLM(BaseLLM):
    def __init__ (self):
        self.client = OpenAI(
            api_key=os.getenv("ZAI_API_KEY"),
            base_url="https://open.bigmodel.cn/api/paas/v4/"
        )

        self.model_name = "glm-5.2"
        self.log = LLMlogger()


    def stream(
        self, 
        userQuery: str, 
        history,
        system_prompt: str = None,
        previousID = None
    ):

        # Log Initialization
        self.log.start(self.model_name)
        start_time = time.time()

        # Log userQuery
        self.log.query(self.model_name, userQuery)

        try:
            interaction = self.client.chat.completions.create(
                model = self.model_name,
                messages = history,
                stream = True,
                temperature = 0.8,
                extra_body={
                    "thinking": {
                        "type": "disabled",
                    },
                },
                max_tokens=1000
            )
        except Exception as e:
            yield LLMEvent(
                type="error",
                data=str(e)
            )
            self.log.error(self.model_name, 0, e)
            return

        queue = Queue()

        def consume_stream():
            token_count = -1 # Indicate Error
            assistant_response = ""
            try:
                # TODO: Add timeout feature (30s for initial, 10s for chunks)
                for event in interaction:
                    # Get Text Response
                    if event.choices[0].delta.content:
                        text = event.choices[0].delta.content
                        assistant_response += text

                        queue.put(LLMEvent(type = "text", data = text))

                    if (hasattr(event, "usage") and event.usage):
                        token_count = event.usage.total_tokens
            
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