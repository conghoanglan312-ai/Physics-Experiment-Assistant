import logging
import os
class LLMlogger():
    def __init__(self):
        self.log = logging.getLogger("LLM")
        self.log.setLevel(logging.INFO)

        # Create log directory if it does not exist
        log_dir = "llm/logs"

        if not os.path.exists(log_dir):
            os.makedirs(log_dir)


        log_file = os.path.join(
            log_dir,
            "LLM.log"
        )

        # Prevent duplicate handlers
        if not self.log.handlers:
            file_handler = logging.FileHandler(log_file)

            formatter = logging.Formatter(
                "%(asctime)s | %(levelname)s | %(message)s"
            )

            file_handler.setFormatter(formatter)

            self.log.addHandler(file_handler)


    def start(self, modelName):
        self.log.info(
            f"model={modelName} "
            "status=started"
        )

    def finish(self, modelName, latency, tokenUsed):
        self.log.info(
            f"model={modelName} "
            f"latency={latency:.2f} "
            f"tokens={tokenUsed} "
            "status=success"
        )

    def error(self, modelName, latency, e):
        self.log.error(
            f"model={modelName} "
            f"latency={latency:.2f} "
            "status=error "
            f"message={str(e)}"
        )

    def query(self, modelName, userQuery):
        self.log.info(
            f"model={modelName} "
            f"query_length={len(userQuery)} "
            "status=success"
        )