import asyncio
import weakref
from enum import StrEnum
from typing import Any, Self

from aiolimiter import AsyncLimiter
from pydantic import BaseModel, PrivateAttr
from shared.agent.chat_openai_async import ChatOpenAI, OutputConfig, OutputConfigKind
from shared.chunking.text_splitter import split_text
from shared.inspector.utils.dag import LiteNode, NodeKind
from shared.prompts.structured_prompting import (
    GENERAL_STE_STYLE_INSTRUCTION,
    Component,
    Prompt,
)

_OPENAI_SEMS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_OPENAI_RATE_LIMITERS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, AsyncLimiter
] = weakref.WeakKeyDictionary()
CHUNK_SIZE_LIMIT = 96_000
MAX_CONCURRENT_OPENAI_REQUESTS = 200
MAX_OPENAI_REQUESTS_PER_SECOND = 50


def _get_semaphore() -> asyncio.Semaphore:
    """Get or create a semaphore for the current event loop."""
    loop = asyncio.get_running_loop()
    if loop not in _OPENAI_SEMS:
        _OPENAI_SEMS[loop] = asyncio.Semaphore(MAX_CONCURRENT_OPENAI_REQUESTS)
    return _OPENAI_SEMS[loop]


def _get_rate_limiter() -> AsyncLimiter:
    """Get or create a rate limiter for the current event loop."""
    loop = asyncio.get_running_loop()
    if loop not in _OPENAI_RATE_LIMITERS:
        _OPENAI_RATE_LIMITERS[loop] = AsyncLimiter(MAX_OPENAI_REQUESTS_PER_SECOND, 1)
    return _OPENAI_RATE_LIMITERS[loop]


def _clip_prompt(p: str, chunk_size: int) -> str:
    prompt_chunks = split_text(p, chunk_size=chunk_size, chunk_overlap=0)
    if len(prompt_chunks) > 1:
        return prompt_chunks[0].text
    else:
        return p


ENTRY_POINT_PREAMBLE = Component(
    string="""
An entry point is a canonical place where users or systems begin interacting with a codebase. It is a primary module or file that acts as a starting point for execution or consumption. There may be only one obvious/major entry point for a codebase or there may be more than one. Identifying an entry point is context-specific, e.g., depending on the implementation language, kind of codebase (library or executable, etc.). Here are some examples in different contexts:

- For executables, an entry point typically includes the definition of a `main()` function (or context-specific analog) or definition of the command line interface (CLI).
- For libraries, an entry point could be the root module that exposes the public API surface (e.g., as curated through reexports or a prelude module). Be careful of identifying trivial reexport files, though. For example, in Python `__init__.py` files are used empty to indicate a module and sometimes populated with just re-exported content. Anything like the former should definitely not be considered an entry point, and the latter may be, but the actual implementation files would be better.
- In some contexts, the major entry point(s) may be script files (shell scripts or language-specific scripts such as an `app.py` or `run.rb` for example) that are run or invoked as an integral part of using the codebase.
- Focus on entry points important for a human user to review when onboarding to a codebase and navigating its contents. Particularly long and complex build files or related files may be important to the codebase and its operation, but may not be very useful for a human to review and understand the flow of execution in the code itself. So you should de-prioritize build files, complex build/dependency configuration files, and entities like Makefiles.

An entry point can look like many things, but it is undoubtedly a focal point for interacting with and understanding a codebase. We wanto identify entry points to help orient developers to how the system is intended to be used and good places to get started looking at documentation and/or source code.
"""
)


async def bounded_llm_generate(
    llm: ChatOpenAI,
    system_prompt: str,
    user_prompt: str,
    sem: asyncio.Semaphore,
    rate_limiter: AsyncLimiter,
    output_cfg: OutputConfig = OutputConfig.default(),
) -> str:
    async with sem, rate_limiter:
        return await llm.generate_response(
            system_prompt=system_prompt, user_prompt=user_prompt, output_cfg=output_cfg
        )


class EntryPointRelevance(StrEnum):
    VeryRelevant = "very_relevant"
    PossiblyRelevant = "possibly_relevant"
    NotLikelyRelevant = "not_likely_relevant"


class RelevanceFlag(BaseModel):
    flag: EntryPointRelevance

    @staticmethod
    def system_prompt() -> str:
        identity_preamble = """
You are an expert software engineer and technical writer that specializes in identifying entry points in software engineering codebases.
"""
        task_description = """
Your job is to review the technical documentation about a source code file and decide if it contains an entry point for the codebase. Specifically, you are to decide which of the following categories it belongs to:

**very_relevant**: This means the file or a specific symbol inside of it, such as a function, is highly likely to be an important entry point for the codebase.

**possibly_relevant**: This means the file or a specific symbol inside of it, such as a function, seems relevant when considering entry points for the codebase, but it's not necessarily very clear that it is an integral entry point. Further context is needed.

**not_likely_relevant**: This means the file as a whole nor any specific symbol inside of it, such as a function, represent a core entry point to the codebase.

Entry points are few and far between in a codebase -- most files or code in files does not correspond to an entry point. Be selective in what you mark as very relevant or possibly relevant. Code may be important and integral to a codebase but still not be an _entry point_ into the codebase.

You will be given exhaustive technical documentation for a specific file and will respond only with your categorization for the relevance of this file as an entry point.
"""
        return (
            Prompt.empty()
            .append(Component(string=identity_preamble))
            .append(ENTRY_POINT_PREAMBLE)
            .append(Component(string=task_description))
            .into_str()
        )

    @classmethod
    async def from_llm(
        cls, llm: ChatOpenAI, root_rel_path: str, long_description: str
    ) -> Self:
        system_prompt = cls.system_prompt()
        user_prompt = _clip_prompt(
            p=f"File (`{root_rel_path}`) technical documentation:\n{long_description}",
            chunk_size=CHUNK_SIZE_LIMIT,
        )
        content_raw = await bounded_llm_generate(
            llm=llm,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            sem=_get_semaphore(),
            rate_limiter=_get_rate_limiter(),
            output_cfg=OutputConfig(kind=OutputConfigKind.JSON_STRICT, payload=cls),
        )
        return cls.parse_raw(content_raw)


class EntryPointCandidate(BaseModel):
    _root_rel_path: str = PrivateAttr()
    _relevance: EntryPointRelevance = PrivateAttr()
    description: str
    rationale: str

    @staticmethod
    def system_prompt() -> str:
        identity_preamble = """
You are an expert software engineer and technical writer that specializes in identifying and describing entry points to a codebase.
"""

        task_description = """
You will be given long form technical documentation, including documentation of all important symbols, for a file in a codebase that has been flagged as potentially relevant as an entry point. Your job is to provide a description of the entry point in no more than 2 -- 3 sentences as well as a detailed rationale for why this is (or is not) a strong candidate for being an entry point for the enclosing codebase. You will also be given whether the file was previously flagged as highly likely to be an entry point or only possibly an entry point. Consider this flagging in your analysis but focus on make your own detailed assessment.

Entry points are few and far between in a codebase -- most files or code in files does not correspond to an entry point. Be selective in what you describe and rationalize as an entry point. Code may be important and integral to a codebase but still not be an _entry point_ into the codebase.
"""

        return (
            Prompt.empty()
            .append(Component(string=identity_preamble))
            .append(ENTRY_POINT_PREAMBLE)
            .append(Component(string=task_description))
            .append(GENERAL_STE_STYLE_INSTRUCTION)
            .into_str()
        )

    @classmethod
    async def from_llm(
        cls,
        llm: ChatOpenAI,
        root_rel_path: str,
        relevance: RelevanceFlag,
        long_description: str,
    ) -> Self:
        system_prompt = cls.system_prompt()
        user_prompt = _clip_prompt(
            p=f"File (`{root_rel_path}`) with entry point relevance flag:\n{relevance.flag.value}\n\nTechnical documentation:\n{long_description}",
            chunk_size=CHUNK_SIZE_LIMIT,
        )
        content_raw = await bounded_llm_generate(
            llm=llm,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            sem=_get_semaphore(),
            rate_limiter=_get_rate_limiter(),
            output_cfg=OutputConfig(kind=OutputConfigKind.JSON_STRICT, payload=cls),
        )
        candidate = cls.parse_raw(content_raw)
        candidate._root_rel_path = root_rel_path
        candidate._relevance = relevance.flag

        return candidate


class FinalizedEntryPoint(BaseModel):
    path: str
    single_sentence_description: str
    rationale: str

    def __str__(self) -> str:
        return f"{self.path}: {self.single_sentence_description}\nRationale: {self.rationale}"


class EntryPoints(BaseModel):
    entry_points: list[FinalizedEntryPoint]

    def __str__(self) -> str:
        return "\n".join([str(e) for e in self.entry_points])

    def entry_point_paths(self) -> str:
        return ", ".join([e.path for e in self.entry_points])

    @staticmethod
    def system_prompt(n: int) -> str:
        identity_preamble = """
You are an expert software engineer and technical writer that specializes in identifying and describing entry points to a codebase.
"""

        task_description_template = """
You will be given a report on candidate entry points derived previously. This report will include a sequential list of candidates with the following information for each: 1) the path of the file that itself, or via some symbol such as a main file internal to the file, represents a candidate for an entry point into the enclosing codebase, 2) A description of the entry point (how it is an entry point), and 3) a rationale for why this is (or is not) a strong candidate for being considered an entry point.

Your job is to decide, from this list of candidates, which are the top {n} entry points. You have all of the candidate entry points and their rationales/descriptions available, so you must decide in comparison what the most important entry points are. You **must** return a list of **no more than {n}** entry points. You can (and should) return fewer than {n} entry points in your finalized list if there are not a full set of {n} entry points for this codebase. For example, a small codebase may have only a single entry point. Remember, in general, entry points are few and far between in a codebase -- most files or code in files does not correspond to an entry point (it may be important or integral but not an _entry point_).

Your output will be a list of finalized entry points with three pieces of information for each of your choice of finalized entry points:
- The path of the file. This is stated in the report information given to you.
- A terse single single sentence. Do not recapitulate the name of the file or the fact that this is an entry point. Be direct and terse in describing why this is an entry point. If applicable and important, however, name a particular component of the file (e.g., the `main` function) that represents the entry point in particular. Otherwise, if the file as a whole is the entry point, either just lead with an action word describing the nature of the entry point. For example: "Initializes ...", "Configures ...", "Defines ..." not "The file initializes ...", "It configures ...", "This file defines ...". Alternatively, you can start with "A script that ..." or "A module that ..." if you can identify the kind of file effectively. Prefer starting any such description with "A" rather than "The" or "This".
- Your rationale and justification for why this is a critical and clear entry point for the codebase in no more than one paragraph.
"""

        return (
            Prompt.empty()
            .append(Component(string=identity_preamble))
            .append(ENTRY_POINT_PREAMBLE)
            .append(Component(string=task_description_template.format(n=n)))
            .append(GENERAL_STE_STYLE_INSTRUCTION)
            .into_str()
        )

    @classmethod
    async def from_llm(
        cls,
        llm: ChatOpenAI,
        docs: dict[LiteNode, dict[str, Any]],
        n: int = 3,
    ) -> Self:
        # Tag files for relevance to the concept of an entry point.
        async with asyncio.TaskGroup() as tg:
            relevance_coros = []
            for node, ir_data in docs.items():
                match node.kind:
                    case NodeKind.ROOT_FOLDER | NodeKind.SUB_FOLDER:
                        continue
                    case NodeKind.FILE:
                        relevance_coros.append(
                            (
                                node,
                                ir_data,
                                tg.create_task(
                                    RelevanceFlag.from_llm(
                                        llm=llm,
                                        root_rel_path=node.root_rel_path,
                                        long_description=ir_data["long"],
                                    )
                                ),
                            )
                        )
                    case _:
                        raise ValueError("Unreachable")
        relevance_list = [(n, i, c.result()) for n, i, c in relevance_coros]

        # Iterate over relevant-tagged files and generate a deeper description and rationale.
        async with asyncio.TaskGroup() as tg:
            candidate_coros = []
            for node, ir_data, relevance in relevance_list:
                match relevance.flag:
                    case EntryPointRelevance.NotLikelyRelevant:
                        continue
                    case (
                        EntryPointRelevance.VeryRelevant
                        | EntryPointRelevance.PossiblyRelevant
                    ):
                        candidate_coros.append(
                            tg.create_task(
                                EntryPointCandidate.from_llm(
                                    llm=llm,
                                    root_rel_path=node.root_rel_path,
                                    relevance=relevance,
                                    long_description=ir_data["long"],
                                )
                            )
                        )
                    case _:
                        raise ValueError("Unreachable")
        candidate_list = [c.result() for c in candidate_coros]

        # Aggregate over all candidates and finalize the set of N top entry points, with associated descriptions/rationale.
        system_prompt = cls.system_prompt(n=n)
        user_prompt_structured = Prompt.empty().append(
            Component(string="Entry point candidates:")
        )
        for candidate in candidate_list:
            user_prompt_structured.append(
                Component(
                    string=f"Candidate path: {candidate._root_rel_path}\nDescription:\n{candidate.description}\nRationale:\n{candidate.rationale}"
                )
            )
        # TODO Clipping here could be quite bad to achieving good results. But this would have to
        # be an extremely large codebase/context to do so because that would mean the serialized
        # set of entry point candidate (should be heavily filtered) descriptive data (short for
        # each) would trip the max chunk size. That would have to be a LOT of files and candidates.
        # Possible, but unlikely. Need to investigate further; blindly clipping for now.
        user_prompt = _clip_prompt(
            p=user_prompt_structured.into_str(), chunk_size=CHUNK_SIZE_LIMIT
        )

        content_raw = await bounded_llm_generate(
            llm=llm,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            sem=_get_semaphore(),
            rate_limiter=_get_rate_limiter(),
            output_cfg=OutputConfig(kind=OutputConfigKind.JSON_STRICT, payload=cls),
        )
        return cls.parse_raw(content_raw)
