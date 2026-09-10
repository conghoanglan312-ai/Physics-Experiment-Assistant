# ZhiPu AI (China)

import os
from dotenv import load_dotenv
from openai import OpenAI
import time
from queue import Queue
from threading import Thread
from llm.logs.logger import LLMlogger
from .base import BaseLLM
from llm.util.generateModelMessage import *
from llm.config.settings import MODELS

load_dotenv()
config = MODELS["zai"]

class ZaiLLM(BaseLLM):
    def __init__ (self):
        self.client = OpenAI(
            api_key=os.getenv("ZAI_API_KEY"),
            base_url="https://open.bigmodel.cn/api/paas/v4/"
        )


        self.log = LLMlogger()


    def stream(
        self, 
        userQuery: str, 
        history,
        system_prompt: str = None,
        previousID = None
    ):

        # Log Initialization
        self.log.start(config["model"])
        start_time = time.time()

        # Log userQuery
        self.log.query(config["model"], userQuery)

        try:
            interaction = self.client.chat.completions.create(
                model = config["model"],
                messages = [{"role": "system", "content": system_prompt}, *history],
                stream = config["stream"],
                temperature = config["temperature"],
                extra_body={
                    "thinking": {
                        "type": "disabled",
                    },
                },
                max_tokens=1000
            )
        except Exception as e:
            message = generateErrorMessage(e)
            self.log.error(config["model"], 0, e)
            yield message
            return

        queue = Queue()

        def consume_stream():
            token_count = -1 # Indicate Error
            assistant_response = ""
            try:
                for event in interaction:
                    # Get Text Response
                    if event.choices[0].delta.content:
                        text = event.choices[0].delta.content
                        message = generateTextMessage(text)

                        assistant_response += text

                        queue.put(message)

                    if (hasattr(event, "usage") and event.usage):
                        token_count = event.usage.total_tokens
            
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
