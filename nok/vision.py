"""`--vision` — knock three times, it reads your screen and answers. No talking.

The other three brains all listen first: they open a microphone, wait for you to
finish a sentence, and only then do something. That chain is where this project
broke on real hardware, and it is also the slowest part of every path.

This one deletes the microphone entirely. Triple-knock captures the screen,
sends it to an OpenAI vision model with a fixed instruction, and puts the answer
in a notification. One request, one answer, no conversation, nothing to say out
loud. If there is a question on your screen you get the answer; if there is a
stack trace you get the cause; if there is neither you get one line of what it
is looking at and nothing more.

It exposes the same three methods as the other sessions, so `app.Nok` cannot
tell the backends apart -- it simply never reports a mic level, because there is
no microphone in this path.

The prompt is deliberately blunt about brevity: this lands in a Windows balloon,
which shows about 250 characters. A model that writes five paragraphs into a
notification has produced nothing the user can read.
"""
import asyncio
import os
import time
from dataclasses import dataclass, field

from .logs import log_event

DEFAULT_MODEL = "gpt-4o"
MAX_TOKENS = 300
REQUEST_TIMEOUT_S = 30.0

SYSTEM_PROMPT = (
    "You are looking at a screenshot of the user's screen. They knocked on "
    "their laptop to ask about it and cannot type or speak a question.\n"
    "Work out what they most likely wanted and answer that, directly.\n"
    "- A question on screen: answer it.\n"
    "- An error, stack trace or failing test: name the cause and the fix.\n"
    "- A form, dialog or decision: say what to do.\n"
    "- Anything else: one line saying what is on screen, then stop.\n"
    "Hard limit: 40 words. This is displayed in a desktop notification, not a "
    "chat window. No preamble, no markdown, no 'I can see that'. Just the answer."
)


@dataclass
class VisionStats:
    turns: int = 0
    first_audio_latency: float = 0.0   # named by the session contract; this is
                                       # time-to-answer, the only latency here
    errors: list = field(default_factory=list)
    answer: str = ""


def model_name() -> str:
    """The vision model to call. Override with NOK_VISION_MODEL_OPENAI."""
    return os.environ.get("NOK_VISION_MODEL_OPENAI", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def build_messages(image_data_url: str, question: str = "") -> list:
    """The chat payload. Pure, so the shape is testable without a network."""
    text = question.strip() if question and question.strip() else "What is this?"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]},
    ]


def extract_answer(response) -> str:
    """Pull the text out of a chat completion, tolerating an empty choice.

    A model that returns no content is a real outcome (refusals, filtered
    images), and it must read as "no answer" rather than crashing the session.
    """
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    if isinstance(content, list):  # some SDK versions return content parts
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
            for part in content
        )
    return (content or "").strip()


class VisionSession:
    """One triple-knock: screenshot in, one answer out."""

    def __init__(self, *, api_key=None, on_state=None, on_answer=None,
                 model=None, **_ignored):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.on_state = on_state or (lambda s, d="": None)
        self.on_answer = on_answer or (lambda title, text: None)
        self.model = model or model_name()
        self.stats = VisionStats()
        self._stop = False

    # -- interface -------------------------------------------------------

    def levels(self):
        return 0.0, 0.0   # no microphone, no playback

    def request_stop(self):
        self._stop = True

    async def interrupt(self):
        self.request_stop()

    async def run(self, opening_prompt=None):
        t0 = time.perf_counter()
        self.on_state("ACTING", "reading the screen")
        try:
            from . import screen
            image = await asyncio.to_thread(screen.data_url)
            if self._stop:
                return self.stats

            self.on_state("THINKING", self.model)
            answer = await asyncio.wait_for(
                asyncio.to_thread(self._ask, image, opening_prompt or ""),
                timeout=REQUEST_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log_event("vision_timeout", model=self.model)
            self.stats.errors.append("timeout")
            self.on_state("ERROR", "the model took too long")
            self.on_answer("NOK", "The model took too long to answer.")
            await asyncio.sleep(1.0)
            return self.stats
        except Exception as exc:
            log_event("vision_failed", error=repr(exc))
            self.stats.errors.append(repr(exc))
            self.on_state("ERROR", str(exc))
            self.on_answer("NOK could not look", str(exc))
            await asyncio.sleep(1.2)
            return self.stats

        self.stats.turns = 1
        self.stats.answer = answer
        self.stats.first_audio_latency = time.perf_counter() - t0
        log_event("vision_answer", model=self.model,
                  seconds=round(self.stats.first_audio_latency, 2),
                  chars=len(answer))

        if answer:
            self.on_state("SPEAKING", answer)
            self.on_answer("NOK", answer)
            await asyncio.sleep(1.0)
        else:
            self.on_state("ERROR", "no answer")
            self.on_answer("NOK", "The model returned nothing.")
            await asyncio.sleep(1.0)
        return self.stats

    # -- the call --------------------------------------------------------

    def _ask(self, image_data_url, question):
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        response = client.chat.completions.create(
            model=self.model,
            messages=build_messages(image_data_url, question),
            max_tokens=MAX_TOKENS,
        )
        return extract_answer(response)
