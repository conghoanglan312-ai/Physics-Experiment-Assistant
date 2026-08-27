from abc import ABC, abstractmethod
from dataclasses import dataclass 
from typing import Any

# Define an output format to the front-end
@dataclass
class LLMEvent:
    type: str
    data: Any = None

# Create abstract base class 
class BaseLLM(ABC):
    @abstractmethod
    def stream(
        self,
        userQuery: str,
        system_prompt: str = None,
        previous_id: str = None
    ):
        pass