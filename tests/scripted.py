"""Scripted extraction for tests. A test says which words it means the model to pick (`quote`); the scripted model answers
as a real one must, with the numbers of the passage's sentences that hold them (runner.ChosenClaim). A quote the passage
does not hold becomes a sentence number that does not exist, which the runner refuses, as it would a model's mistake.

    from tests.scripted import Extraction, ExtractedClaim
    Extraction(claims=[ExtractedClaim(predicate="pricing.starter", quote="The Starter plan costs $29 per month.", needed_fact=1)])
"""

import re

from evidence import normalize

NO_SUCH_SENTENCE = 10 ** 6


class ExtractedClaim:
    """What a test means the model to state. `claim` is accepted and ignored: the claim is the quote."""
    def __init__(self, quote: str, needed_fact: int = 1, predicate: str = "p", value: str = "", claim: str = ""):
        self.quote, self.needed_fact, self.predicate, self.value = quote, needed_fact, predicate, value


class Extraction:
    """Resolved against the prompt when the scripted model answers (see `resolve`)."""
    def __init__(self, claims=()):
        self.claims = list(claims)

    def resolve(self, prompt: str):
        from runner import ChosenClaim, Extraction as Real
        units = [(int(n), t) for n, t in re.findall(r"^\[(\d+)\] (.*)$", prompt, re.M)]
        return Real(claims=[ChosenClaim(predicate=c.predicate, sentences=numbers_for(units, c.quote), value=c.value,
                                        needed_fact=c.needed_fact)
                            for c in self.claims])


def numbers_for(units: list[tuple[int, str]], quote: str) -> list[int]:
    """The fewest adjacent sentences (up to three) whose text holds the quote; a fragment too short to state a fact (a lone
    table cell) takes the sentence before it, as the prompt asks a model to do."""
    want = normalize(quote)
    for width in (1, 2, 3):
        for k in range(len(units) - width + 1):
            if want in normalize(" ".join(t for _, t in units[k:k + width])):
                lo, hi = k, k + width
                while len(" ".join(t for _, t in units[lo:hi]).split()) < 4 and hi - lo < 3 and lo > 0:
                    lo -= 1
                return [n for n, _ in units[lo:hi]]
    return [NO_SUCH_SENTENCE]


def resolved(out, prompt: str):
    """What a scripted model returns: a test's Extraction resolved against the prompt, anything else as it is."""
    return out.resolve(prompt) if isinstance(out, Extraction) else out
