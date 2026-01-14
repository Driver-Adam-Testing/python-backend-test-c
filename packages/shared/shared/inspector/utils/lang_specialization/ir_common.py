from __future__ import annotations

import abc
import concurrent.futures
import re
from collections import defaultdict
from math import ceil
from typing import Self

import openai
from pydantic import BaseModel, PrivateAttr
from shared.agent.chat_openai import ChatOpenAI, OutputConfig, OutputConfigKind
from shared.inspector.utils.lang_specialization.symbol_common import (
    RawSymbolCollection,
    RawSymbolData,
    ReifiedSymbol,
    ScopeRelation,
    SymbolKind,
)
from shared.inspector.utils.symbol_table.utils import (
    get_fully_qualified_name,
    is_data_structure,
)
from shared.inspector.utils.threadpool import FastShutdownThreadPoolExecutor

MAX_SYMBOLS_PER_WORKER = 50
MAX_WORKERS_FOR_SYMBOLS = 10


NON_CAPITALIZED_SET = {"and", "with", "for"}


def snake_case_to_spaced_string(snake_case: str) -> str:
    items = []
    split_str = snake_case.split("_")
    if split_str:
        items.append(split_str[0].capitalize())
        for item in split_str[1:]:
            if item.strip().lower() in NON_CAPITALIZED_SET:
                items.append(item)
            else:
                items.append(item.capitalize())
    return " ".join(items).strip()


def ensure_enclosed_with_backticks(raw_str: str) -> str:
    start = "" if raw_str.startswith("`") else "`"
    end = "" if raw_str.endswith("`") else "`"
    return start + raw_str + end


def compute_num_workers(num_symbols: int) -> int:
    return min(
        ceil(num_symbols / MAX_SYMBOLS_PER_WORKER),
        MAX_WORKERS_FOR_SYMBOLS,
    )


def escape_markdown_characters(text: str) -> str:
    markdown_special_chars = r"\`*_{}[]()#+-.!~"
    escaped_text = re.sub(rf"([{re.escape(markdown_special_chars)}])", r"\\\1", text)
    return escaped_text


class MdRenderable(BaseModel, abc.ABC):
    @abc.abstractmethod
    def render_markdown(self, doc_label: str) -> str:
        pass


class RawContent(MdRenderable):
    content: str

    def render_markdown(self, doc_label: str) -> str:
        return f"{self.content}\n"


class RawContentNoNone(MdRenderable):
    content: str | None

    def render_markdown(self, doc_label: str) -> str:
        if self.content is None or len(self.content) == 0:
            return ""
        return f"- **{snake_case_to_spaced_string(doc_label)}**: {self.content}\n"


class FieldNameWithBackTickContent(MdRenderable):
    content: str

    def render_markdown(self, doc_label: str) -> str:
        if len(self.content) == 0:
            return ""
        return f"- **{snake_case_to_spaced_string(doc_label)}**: `{self.content}`\n"


class FieldNameWithBulletedContent(MdRenderable):
    content: str

    def render_markdown(self, doc_label: str) -> str:
        return (
            f"- **{snake_case_to_spaced_string(doc_label)}**:\n    - {self.content}\n"
        )


class FieldNameTypedWithRawContent(MdRenderable):
    type: str
    content: str

    def render_markdown(self, doc_label: str) -> str:
        if len(self.content) == 0:
            return ""
        return f"- **{snake_case_to_spaced_string(doc_label)}**: `{self.type}`: {self.content}\n"


class FieldNameWithRawContent(MdRenderable):
    content: str

    def render_markdown(self, doc_label: str) -> str:
        if len(self.content) == 0:
            return ""
        return f"- **{snake_case_to_spaced_string(doc_label)}**: {self.content}\n"


class NamedContent(MdRenderable):
    name: str
    content: str

    def render_markdown(self, doc_label: str) -> str:
        return f"    - `{self.name}`: {self.content}\n"


class NamedTypedContent(MdRenderable):
    name: str
    content: str
    type: str

    def render_markdown(self, doc_label: str) -> str:
        return f"    - `{self.name}`: `{self.type}` {self.content}\n"


class ListedBacktickNameTypeRawContentNoNone(MdRenderable):
    content: list[NamedTypedContent]

    def render_markdown(self, doc_label: str) -> str:
        output_str = ""
        if len(self.content) > 0:
            output_str += f"- **{snake_case_to_spaced_string(doc_label)}**:\n"
            for item in self.content:
                output_str += item.render_markdown(doc_label)
        return output_str


class ListedBacktickNameTypeRawContentWithNone(MdRenderable):
    content: list[NamedTypedContent]

    def render_markdown(self, doc_label: str) -> str:
        output_str = ""
        output_str += f"- **{snake_case_to_spaced_string(doc_label)}**:\n"
        if len(self.content) > 0:
            for item in self.content:
                output_str += item.render_markdown(doc_label)
        else:
            output_str += " None\n"
        return output_str


class ListedBacktickNameRawContentNoNone(MdRenderable):
    content: list[NamedContent]

    def render_markdown(self, doc_label: str) -> str:
        output_str = ""
        if len(self.content) > 0:
            output_str += f"- **{snake_case_to_spaced_string(doc_label)}**:\n"
            for item in self.content:
                output_str += item.render_markdown(doc_label)
        return output_str


class FourHeaderNamedContentNoNone(MdRenderable):
    content: list[NamedContent]

    def render_markdown(self, doc_label: str) -> str:
        output_str = "\n**Group Elements**\n"
        if len(self.content) > 0:
            for item in self.content:
                output_str += f"#### {item.name}\n{item.content}\n"
        return output_str


class ListedBacktickNameRawContentWithNone(MdRenderable):
    content: list[NamedContent]

    def render_markdown(self, doc_label: str) -> str:
        output_str = ""
        output_str += f"- **{snake_case_to_spaced_string(doc_label)}**:"
        if len(self.content) > 0:
            output_str += "\n"
            for item in self.content:
                output_str += item.render_markdown(doc_label)
        else:
            output_str += " None\n"
        return output_str


class ListedCommaCombinedBackTickRawContentNoNone(MdRenderable):
    content: list[str]

    def render_markdown(self, doc_label: str) -> str:
        output_str = ""
        if len(self.content) > 0:
            output_str += f"- **{snake_case_to_spaced_string(doc_label)}**:"
            for item in self.content[:-1]:
                output_str += f" {ensure_enclosed_with_backticks(item)},"
            last_item = self.content[-1]
            output_str += f" {ensure_enclosed_with_backticks(last_item)}\n"
        return output_str


class ListedCommaCombinedBackTickRawContentWithNone(MdRenderable):
    content: list[str]

    def render_markdown(self, doc_label: str) -> str:
        output_str = ""
        output_str += f"- **{snake_case_to_spaced_string(doc_label)}**:"
        if len(self.content) > 0:
            for item in self.content[:-1]:
                output_str += f" {ensure_enclosed_with_backticks(item)},"
            last_item = self.content[-1]
            output_str += f" {ensure_enclosed_with_backticks(last_item)}\n"
        else:
            output_str += " None\n"
        return output_str


class ListedBackTickRawContentNoNone(MdRenderable):
    content: list[str]

    def render_markdown(self, doc_label: str) -> str:
        output_str = ""
        if len(self.content) > 0:
            output_str += f"- **{snake_case_to_spaced_string(doc_label)}**:\n"
            for item in self.content:
                output_str += f"    - {ensure_enclosed_with_backticks(item)}\n"
        return output_str


class ListedBackTickRawContentWithNone(MdRenderable):
    content: list[str]

    def render_markdown(self, doc_label: str) -> str:
        output_str = ""
        output_str += f"- **{snake_case_to_spaced_string(doc_label)}**:"
        if len(self.content) > 0:
            output_str += "\n"
            for item in self.content:
                output_str += f"    - {ensure_enclosed_with_backticks(item)}\n"
        else:
            output_str += " None\n"
        return output_str


class ListedRawContentNoNone(MdRenderable):
    content: list[str]

    def render_markdown(self, doc_label: str) -> str:
        output_str = ""
        if len(self.content) > 0:
            output_str += f"- **{snake_case_to_spaced_string(doc_label)}**:\n"
            for item in self.content:
                output_str += f"    - {item}\n"
        return output_str


class ListedRawContentWithNone(MdRenderable):
    content: list[str]

    def render_markdown(self, doc_label: str) -> str:
        output_str = ""
        output_str += f"- **{snake_case_to_spaced_string(doc_label)}**:"
        if len(self.content) > 0:
            output_str += "\n"
            for item in self.content:
                output_str += f"    - {item}\n"
        else:
            output_str += " None\n"
        return output_str


class NestedListedRawContent(MdRenderable):
    content: list[list[str]]

    def render_markdown(self, doc_label: str) -> str:
        output_str = ""
        output_str += f"- **{snake_case_to_spaced_string(doc_label)}**:"
        if len(self.content) > 0:
            output_str += "\n"
            for idx, item in enumerate(self.content):
                output_str += f"    - Block {idx+1}\n"
                for subitem in item:
                    output_str += f"        - {subitem}\n"
        else:
            output_str += " None\n"
        return output_str


class ListData(BaseModel):
    data: list[str]

    @classmethod
    def from_llm(
        cls, llm: ChatOpenAI, system_prompt: str, user_prompt: str, code: str
    ) -> Self:
        user_prompt_complete = f"{user_prompt}\n\nCode:\n\n{code}"
        content_raw = llm.generate_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt_complete,
            output_cfg=OutputConfig(kind=OutputConfigKind.JSON_STRICT, payload=cls),
        )
        if content_raw is None:
            return None

        return cls.parse_raw(content_raw)

    def render_markdown(self) -> str:
        output = ""
        output += "\n---\n"
        for dep in self.data:
            output += f"- `{dep}`\n"
        output += "\n"
        return output

    def __str__(self) -> str:
        return self.render_markdown()


class IrData(BaseModel, abc.ABC):
    _children: list = PrivateAttr(default_factory=list)
    _supported_child_ordering: list[ScopeRelation] = PrivateAttr(
        default=[]
    )  # provide a list of field names in the order they should be rendered
    # Support for children and how they are presented are now defined by _supported_child_ordering and
    # _child_to_ir and _child_to_field_name, rather than as explicit fields here.

    _reified_symbol: ReifiedSymbol | None = PrivateAttr(default=None)

    @classmethod
    @abc.abstractmethod
    def system_prompt(cls, symbol: RawSymbolData) -> str:
        pass

    @classmethod
    @abc.abstractmethod
    def user_prompt(cls, symbol: RawSymbolData) -> str:
        pass

    @classmethod
    @abc.abstractmethod
    def child_to_ir(cls, symbol: RawSymbolData) -> type[IrData] | None:
        # when constructing this method - if you have a field that you just want listed with no
        # additional IR content needed, return None from this function when that type of symbol is passed
        # e.g. nested classes that are just listed
        pass

    @classmethod
    @abc.abstractmethod
    def child_to_field_name(cls, child: RawSymbolData) -> str:
        pass

    @classmethod
    @abc.abstractmethod
    def default_instance(cls, reified_symbol: ReifiedSymbol | None = None) -> Self:
        pass

    def _apply_bespoke_data(self) -> None:
        """
        This method can be overridden in subclasses to apply any bespoke data processing
        that is specific to the subclass.
        """

    @classmethod
    def from_llm(
        cls,
        llm: ChatOpenAI,
        symbol: RawSymbolData,
    ) -> Self:
        if symbol.symbol_code is None:
            # handle C++ classes defined in header
            cls_instance = cls.default_instance(reified_symbol=symbol.reified_symbol)
        else:
            try:
                system_prompt = cls.system_prompt(symbol)
                if llm.model == "gpt-4o-mini":
                    system_prompt += "\n\nWhen referencing any code entities (e.g. functions, classes, structures, variables, etc.), enclose the entity name in backticks (`)."
                content_raw = llm.generate_response(
                    system_prompt=system_prompt,
                    user_prompt=cls.user_prompt(symbol),
                    output_cfg=OutputConfig(
                        kind=OutputConfigKind.JSON_STRICT, payload=cls
                    ),
                )
                if content_raw is None:
                    return None

                # Kept here for debugging in the future.
                # print("System Prompt: ", system_prompt)
                # print("User Prompt: ", cls.user_prompt(symbol))
                # print("Content Raw: ", content_raw)

            except openai.LengthFinishReasonError as _:
                print("LengthFinishReasonError caught")
                return None
                # TODO: do something with this - switch to default_instance
            cls_instance = cls.parse_raw(content_raw)

        if symbol.reified_symbol is not None:
            cls_instance._reified_symbol = (
                symbol.reified_symbol
            )  # So we can render this info later

        if len(symbol.children) > 0:
            workers = compute_num_workers(len(symbol.children))
            futures = {}

            with FastShutdownThreadPoolExecutor(max_workers=workers) as executor:
                for idx, child_symbol in enumerate(symbol.children):
                    child_ir_cls = cls.child_to_ir(child_symbol)
                    if child_ir_cls is None:
                        cls_instance._children.append((child_symbol, None))
                    else:
                        futures[
                            executor.submit(child_ir_cls.from_llm, llm, child_symbol)
                        ] = [idx, child_symbol]
                results = []
                for idx, future in enumerate(
                    concurrent.futures.as_completed(futures.keys())
                ):
                    res = future.result()
                    if res is not None:
                        print(
                            f"Processed {idx} / {len(futures)} children for {symbol.name}"
                        )
                        results.append([futures[future], res])
                for result in sorted(results, key=lambda tup: tup[0][0]):
                    cls_instance._children.append((result[0][1], result[1]))

        return cls_instance

    def render_markdown(self) -> str:
        self._apply_bespoke_data()
        output = ""

        # doesn't handle children, since children is a private attribute
        # for label_name, label_content in self:
        if self._reified_symbol is not None:
            # add link to source code
            path_part = self._reified_symbol.raw.file_path
            line_num_part = f"L{self._reified_symbol.raw.start_line}-L{self._reified_symbol.raw.end_line}"
            link = f"[View Source →](<{path_part}#{line_num_part}>)\n\n"
            output += link
        for label_name in self.__annotations__:
            label_content = getattr(self, label_name, None)
            if not isinstance(label_content, MdRenderable):
                continue
                # raise ValueError(
                #     f"Unsupported field content type for {label_name}: {type(label_content)}. "
                #     f"Add a MdRenderable class to render this content."
                # )

            rendered = label_content.render_markdown(label_name)

            if (
                self._reified_symbol
                and self._reified_symbol.raw.symbol_kind == SymbolKind.CALLABLE
                and self._reified_symbol.calls
            ):
                seen = set()
                for called_func in self._reified_symbol.calls:
                    kind_part = called_func.raw.symbol_kind.name.lower()
                    name_part = re.escape(called_func.raw.name)  # escape special chars
                    fqn = get_fully_qualified_name(called_func.raw)
                    path_part = called_func.raw.file_path

                    if fqn in seen:
                        continue
                    seen.add(fqn)
                    link = f"[`{name_part}`](<{path_part}#{kind_part}:{fqn}>)"

                    rendered = re.sub(rf"`{name_part}`", link, rendered)

            output += rendered
        if self._reified_symbol is not None:
            sym = self._reified_symbol
            if sym.raw.symbol_kind == SymbolKind.CALLABLE and sym.calls:
                callables_label = (
                    "Functions Called"
                    if sym.raw.file_path.suffix not in [".cs", ".rb"]
                    else "Methods Called"
                )
                output += f"- **{callables_label}**:\n"
                seen_name_parts = defaultdict(list)
                for called_func in self._reified_symbol.calls:
                    fqn = get_fully_qualified_name(called_func.raw)
                    seen_name_parts[fqn].append(called_func)
                for fqn, calls in seen_name_parts.items():
                    kind_part = calls[0].raw.symbol_kind.name.lower()
                    path_part = calls[0].raw.file_path

                    output += f"    - [`{fqn}`](<{path_part}#{kind_part}:{fqn}>)\n"
            if (
                sym.raw.symbol_kind == SymbolKind.CALLABLE_DECLARATION
                and sym.definition is not None
            ):
                kind_part = sym.definition.raw.symbol_kind.name.lower()
                fqn = get_fully_qualified_name(sym.definition.raw)
                path_part = sym.definition.raw.file_path

                output += f"- **See Also**: [`{fqn}`](<{path_part}#{kind_part}:{fqn}>)  (Implementation)\n"

            if sym.raw.symbol_kind == SymbolKind.CALLABLE and sym.parent is not None:
                # Link member functions to their object definiton
                kind_part = sym.parent.raw.symbol_kind.name.lower()
                fqn = get_fully_qualified_name(sym.parent.raw)
                path_part = sym.parent.raw.file_path
                parent_label = (
                    "Base Class"
                    if sym.parent.raw.symbol_kind == SymbolKind.CLASS
                    else "Data Structure"
                )

                if sym.raw.file_path.suffix == ".go":
                    output += f"- **See also**: [`{fqn}`](<{path_part}#{kind_part}:{fqn}>)  (Receiver Type)\n"
                else:
                    output += f"- **See also**: [`{fqn}`](<{path_part}#{kind_part}:{fqn}>)  ({parent_label})\n"
            if is_data_structure(sym.raw):
                member_label = (
                    "Methods"
                    if sym.raw.symbol_kind == SymbolKind.CLASS
                    or sym.raw.file_path.suffix == ".go"
                    else "Member Functions"
                )
                inherit_label = (
                    "Inherits From"
                    if sym.raw.file_path.suffix
                    not in [".java", ".ts", ".js", ".tsx", ".jsx"]
                    else "Extends/Implements"
                )
                callable_children = [
                    child
                    for child in sym.children
                    if child.raw.symbol_kind == SymbolKind.CALLABLE
                ]
                if (
                    len(callable_children) > 0
                    and sym.raw.symbol_kind != SymbolKind.INTERFACE
                ):
                    output += f"- **{member_label}**:\n"
                    for child_symbol in callable_children:
                        fqn = get_fully_qualified_name(child_symbol.raw)
                        kind_part = child_symbol.raw.symbol_kind.name.lower()
                        path_part = child_symbol.raw.file_path
                        output += f"    - [`{fqn}`](<{path_part}#{kind_part}:{fqn}>)\n"
                if sym.inherits_from is not None and len(sym.inherits_from) > 0:
                    output += f"- **{inherit_label}**:\n"
                    for inherited_class in sym.inherits_from:
                        kind_part = inherited_class.raw.symbol_kind.name.lower()
                        fqn = get_fully_qualified_name(inherited_class.raw)
                        path_part = inherited_class.raw.file_path
                        output += f"    - [`{fqn}`](<{path_part}#{kind_part}:{fqn}>)\n"
                elif (
                    sym.raw.base_class_names is not None
                    and len(sym.raw.base_class_names) > 0
                ):
                    output += f"- **{inherit_label}**:\n"
                    for base_class_name in sym.raw.base_class_names:
                        output += f"    - `{base_class_name}`\n"
            # if sym.raw.symbol_kind == SymbolKind.CALLABLE and sym.usages:
            #     output += "- **Usages of this function**:\n"
            #     for usage in self._reified_symbol.usages:
            #         path_part = usage.raw.file_path
            #         file_name = usage.raw.file_path.name
            #         start_line = usage.raw.start_line
            #         # end_line = usage.raw.end_line

            #         output += f"    - [{file_name}]({path_part}) (line {start_line})\n"
        # Render child data
        child_dictionary = {
            label_name: "" for label_name in self._supported_child_ordering
        }
        for child_symbol, child_content in self._children:
            label_name = self.child_to_field_name(child_symbol)
            if len(child_dictionary[label_name]) == 0:
                child_dictionary[label_name] += f"\n**{label_name}**\n"

            if label_name not in child_dictionary:
                raise ValueError(f"Unsupported child field name: {label_name}")

            if child_content is not None:
                scoped_name = (
                    child_symbol.scope.split(child_symbol.delimiter)[-1]
                    + child_symbol.delimiter
                    + child_symbol.name
                    if child_symbol.scope
                    else child_symbol.name
                )
                if child_content._reified_symbol is not None:
                    kind_part = (
                        child_content._reified_symbol.raw.symbol_kind.name.lower()
                    )
                    fqn = get_fully_qualified_name(child_content._reified_symbol.raw)
                    id_comment = f"<!-- {{{{#{kind_part}:{fqn}}}}} -->"
                else:
                    id_comment = ""
                scoped_name = escape_markdown_characters(scoped_name)
                child_dictionary[label_name] += (
                    f"\n---\n#### {scoped_name}{id_comment}\n"
                )
                child_dictionary[label_name] += child_content.render_markdown()
            else:
                if child_symbol.reified_symbol is not None:
                    kind_part = child_symbol.reified_symbol.raw.symbol_kind.name.lower()
                    fqn = get_fully_qualified_name(child_symbol.reified_symbol.raw)
                    id_comment = f"<!-- {{{{#{kind_part}:{fqn}}}}} -->"
                else:
                    id_comment = ""
                child_dictionary[label_name] += f"- `{child_symbol.name}`{id_comment}\n"

        for _, label_content in child_dictionary.items():
            output += label_content

        output += "\n"
        return output


class IrCollection(BaseModel, abc.ABC):
    data: dict[str, IrData | list[IrData]]

    @classmethod
    def from_llm_with_ir_data(
        cls,
        ir_data: type[IrData],
        llm: ChatOpenAI,
        symbols_list: RawSymbolCollection,
    ) -> Self:
        symbols_dict = {}
        futures = {}
        workers = compute_num_workers(len(symbols_list.data))
        print("Num workers: ", workers)

        with FastShutdownThreadPoolExecutor(max_workers=workers) as executor:
            for _, s in symbols_list.data.items():
                if isinstance(s, list):
                    for raw_sym_data in s:
                        if raw_sym_data.name not in symbols_dict:
                            symbols_dict[raw_sym_data.name] = []
                            # can reattach raw symbol info to use downstream when rendering MD.
                        futures[
                            executor.submit(ir_data.from_llm, llm, raw_sym_data)
                        ] = raw_sym_data.name
                elif isinstance(s, RawSymbolData):
                    if s.name not in symbols_dict:
                        symbols_dict[s.name] = []
                    futures[executor.submit(ir_data.from_llm, llm, s)] = s.name
                else:
                    raise ValueError("Unsupported type in RawSymbolCollection")

            for idx, future in enumerate(
                concurrent.futures.as_completed(futures.keys())
            ):
                res = future.result()
                if res is not None:
                    print(f"Processed {idx}/{len(futures)} symbols")
                    symbols_dict[futures[future]].append(res)

        return cls(
            data=symbols_dict
        )  # This is just the LLM produced content, without the raw symbol info.

    @classmethod
    @abc.abstractmethod
    def from_llm(
        cls,
        llm: ChatOpenAI,
        symbols_list: RawSymbolCollection,
    ) -> Self:
        pass

    def render_markdown(self) -> str:
        output = ""
        for k, v in self.data.items():
            k = escape_markdown_characters(k)
            for item in v:
                if item._reified_symbol is not None:
                    kind_part = item._reified_symbol.raw.symbol_kind.name.lower()
                    name_part = get_fully_qualified_name(item._reified_symbol.raw)
                    id_comment = f"<!-- {{{{#{kind_part}:{name_part}}}}} -->"
                else:
                    id_comment = ""

                output += f"\n---\n### {k}{id_comment}\n"
                output += item.render_markdown()
        return output

    def __str__(self) -> str:
        return self.render_markdown()


### Default Classes, must still be inherited from, or create new ones for any specialization ###
class VariableData(IrData):
    type: FieldNameWithBackTickContent
    description: FieldNameWithRawContent
    use: FieldNameWithRawContent

    @classmethod
    def default_instance(cls, reified_symbol: ReifiedSymbol | None = None) -> Self:
        return cls(
            type=FieldNameWithBackTickContent(content=""),
            description=FieldNameWithRawContent(content=""),
            use=FieldNameWithRawContent(content=""),
        )


class DataStructureData(IrData, abc.ABC):
    type: FieldNameWithBackTickContent
    members: ListedBacktickNameRawContentNoNone
    description: FieldNameWithRawContent

    @classmethod
    def default_instance(cls, reified_symbol: ReifiedSymbol | None = None) -> Self:
        return cls(
            type=FieldNameWithBackTickContent(content=""),
            members=ListedBacktickNameRawContentNoNone(content=[]),
            description=FieldNameWithRawContent(content=""),
        )


class FnDeclData(IrData, abc.ABC):
    single_sentence: RawContent
    description: FieldNameWithRawContent
    inputs: ListedBacktickNameRawContentWithNone
    output: FieldNameWithRawContent

    @classmethod
    def default_instance(cls, reified_symbol: ReifiedSymbol | None = None) -> Self:
        return cls(
            single_sentence=RawContent(content=""),
            description=FieldNameWithRawContent(content=""),
            inputs=ListedBacktickNameRawContentWithNone(content=[]),
            output=FieldNameWithRawContent(content=""),
        )


class FnData(IrData, abc.ABC):
    single_sentence: RawContent
    inputs: ListedBacktickNameRawContentWithNone
    logic_and_control_flow: ListedRawContentWithNone
    output: FieldNameWithRawContent

    @classmethod
    def default_instance(cls, reified_symbol: ReifiedSymbol | None = None) -> Self:
        return cls(
            single_sentence=RawContent(content=""),
            inputs=ListedBacktickNameRawContentWithNone(content=[]),
            logic_and_control_flow=ListedBacktickNameRawContentWithNone(content=[]),
            output=FieldNameWithRawContent(content=""),
        )


class ClassData(IrData, abc.ABC):
    type: FieldNameWithBackTickContent
    members: ListedBacktickNameRawContentNoNone
    description: FieldNameWithRawContent
    _supported_child_ordering: list[str] = PrivateAttr(
        default=[ScopeRelation.METHOD, ScopeRelation.NESTED_CLASS]
    )

    @classmethod
    def default_instance(cls, reified_symbol: ReifiedSymbol | None = None) -> Self:
        return cls(
            description=FieldNameWithRawContent(content="Implemented elsewhere"),
            type=FieldNameWithBackTickContent(content="N/A"),
            members=ListedBacktickNameRawContentNoNone(content=[]),
            inherits_from=ListedRawContentNoNone(content=[]),
        )
