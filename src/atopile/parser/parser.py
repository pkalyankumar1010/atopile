import logging
from contextlib import contextmanager
from pathlib import Path
from typing import List

import yaml
from antlr4 import CommonTokenStream, FileStream, InputStream

from atopile.model.model import EdgeType, Model, VertexType
from atopile.model.utils import generate_edge_uid
from atopile.parser.AtopileLexer import AtopileLexer
from atopile.parser.AtopileParser import AtopileParser
from atopile.parser.AtopileParserVisitor import AtopileParserVisitor
from atopile.project.project import Project
from atopile.utils import profile

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


class Builder(AtopileParserVisitor):
    def __init__(self, project: Project) -> None:
        self.model = Model()
        self.project = project

        self._block_stack: List[str] = []
        self._file_stack: List[str] = []

        self._tree_cache = {}

        # if something's in parsed files, we must skip building it a second time
        super().__init__()

    @property
    def current_block(self) -> str:
        return self._block_stack[-1]

    @property
    def current_file(self):
        return self._file_stack[0]

    @contextmanager
    def working_block(self, ref: str):
        self._block_stack.append(ref)
        yield
        self._block_stack.pop()

    @contextmanager
    def working_file(self, abs_path: Path):
        std_path = self.project.standardise_import_path(abs_path)
        self._file_stack.append(abs_path)
        with self.working_block(str(std_path)):
            yield
        self._file_stack.pop()
        self.model.src_files.append(std_path)

    def parse_file(self, abs_path: Path):
        if str(abs_path) in self._tree_cache:
            return self._tree_cache[str(abs_path)]

        # FIXME: hacky performance improvement by avoiding jittery read
        with abs_path.open("r", encoding="utf-8") as f:
            input = InputStream(f.read())
        # input = FileStream(abs_path, encoding="utf-8")
        lexer = AtopileLexer(input)
        stream = CommonTokenStream(lexer)
        parser = AtopileParser(stream)
        tree = parser.file_input()
        self._tree_cache[str(abs_path)] = tree
        return tree

    def apply_data_layer(self, data_path: Path):
        with data_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            log.error(f"{data_path} is not a valid data-layer because it doesn't evaluate to a dictionary. Skipping.")
            return

        for k, v in data.items():
            if not isinstance(data, dict):
                log.error(f"{k} from data-layer {data_path} is not a valid data-layer because it doesn't evaluate to a dictionary. Skipping.")
                continue
            try:
                self.model.data[k].update(v)
            except KeyError:
                log.error(f"{k} from data-layer {data_path} not found in model. Data will not be applied.")

    def build(self, path: Path, data_layers: List[Path]) -> Model:
        """
        Start the build from the specified file.
        """
        if not path.exists():
            raise FileNotFoundError(path)

        abs_path = path.resolve().absolute()

        std_path = self.project.standardise_import_path(abs_path)
        std_path_str = str(std_path)

        tree = self.parse_file(abs_path)

        self.model.new_vertex(VertexType.file, std_path_str)
        self.model.data[std_path_str] = {}

        with self.working_file(abs_path):
            self.visit(tree)

        # apply data-layers
        for data_path in data_layers:
            data_path = Path(data_path)
            self.apply_data_layer(data_path)

        return self.model

    def visitImport_stmt(self, ctx: AtopileParser.Import_stmtContext):
        import_filename = self.get_string(ctx.string())

        abs_path, std_path = self.project.resolve_import(import_filename, self.current_file)

        if std_path in self._file_stack:
            raise RuntimeError(f"Circular import detected: {std_path}")

        if std_path not in self.model.src_files:
            # do the actual import, parsing etc...
            with self.working_file(abs_path):
                tree = self.parse_file(abs_path)
                self.model.new_vertex(VertexType.file, import_filename)
                self.model.data[import_filename] = {}
                super().visit(tree)

        # link the import to the current block
        to_import = ctx.name_or_attr().getText()
        graph_path, data_path = self.model.find_ref(to_import, import_filename)
        if data_path:
            raise RuntimeError(f"Cannot import data path {data_path}")

        self.model.new_edge(
            EdgeType.imported_to,
            graph_path,
            self.current_block
        )

        # the super().visit() in the new file import section should
        # handle all depth required. From here, we always want to go back up
        return None

    def define_block(self, ctx, block_type: VertexType):
        name = ctx.name().getText()

        if ctx.OPTIONAL():
            block_path = self.model.new_vertex(
                block_type,
                name,
                option_of=self.current_block
            )
        else:
            block_path = self.model.new_vertex(
                block_type,
                name,
                part_of=self.current_block
            )

        self.model.data[block_path] = {}

        with self.working_block(block_path):
            return super().visitChildren(ctx)

    def visitComponentdef(self, ctx: AtopileParser.ComponentdefContext):
        return self.define_block(ctx, VertexType.component)

    def visitModuledef(self, ctx: AtopileParser.ModuledefContext):
        return self.define_block(ctx, VertexType.module)

    def visitPindef_stmt(self, ctx: AtopileParser.Pindef_stmtContext):
        name = ctx.name().getText()
        pin_path = self.model.new_vertex(
            VertexType.pin,
            name,
            part_of=self.current_block
        )
        self.model.data[pin_path] = {}

        return super().visitPindef_stmt(ctx)

    def visitSignaldef_stmt(self, ctx: AtopileParser.Signaldef_stmtContext):
        name = ctx.name().getText()

        if ctx.PRIVATE():
            private = True
        else:
            private = False

        signal_path = self.model.new_vertex(
            VertexType.signal,
            name,
            part_of=self.current_block
        )
        self.model.data[signal_path] = {
            "private": private,
        }

        return super().visitSignaldef_stmt(ctx)

    def deref_connectable(self, ctx: AtopileParser.ConnectableContext) -> str:
        if ctx.name_or_attr():
            ref = ctx.name_or_attr().getText()
        elif ctx.signaldef_stmt():
            ref = ctx.signaldef_stmt().name().getText()
        elif ctx.pindef_stmt():
            ref = ctx.pindef_stmt().name().getText()
        else:
            raise TypeError("Cannot connect to this type of object")

        cn_path, cn_data = self.model.find_ref(ref, self.current_block)
        if cn_data:
            raise TypeError(f"Cannot connect to data object {ref}")
        return cn_path

    def visitConnect_stmt(self, ctx: AtopileParser.Connect_stmtContext):
        # visit the connectables now before attempting to make a connection
        result = self.visitChildren(ctx)
        from_path = self.deref_connectable(ctx.connectable(0))
        to_path = self.deref_connectable(ctx.connectable(1))
        uid = generate_edge_uid(from_path, to_path, self.current_block)
        self.model.new_edge(EdgeType.connects_to, from_path, to_path, uid=uid)
        self.model.data[uid] = {
            "defining_block": self.current_block,
        }

        # children are already vistited
        return result

    def visitWith_stmt(self, ctx: AtopileParser.With_stmtContext):
        with_ref = ctx.name_or_attr().getText()
        with_path, _ = self.model.find_ref(with_ref, self.current_block)

        self.model.enable_option(with_path)

        return super().visitWith_stmt(ctx)

    def visitAssign_stmt(self, ctx: AtopileParser.Assign_stmtContext):
        assignee = ctx.name_or_attr().getText()
        assignable: AtopileParser.AssignableContext = ctx.assignable()

        if assignable.new_stmt():
            class_ref = assignable.new_stmt().name_or_attr().getText()
            class_path, _ = self.model.find_ref(class_ref, self.current_block)
            # FIXME: this probably throws a dud error if the name is an attr
            # NOTE: we're not using the assignee here because we actually want that error until this is fixed properly
            instance_name = ctx.name_or_attr().name().getText()
            self.model.instantiate_block(class_path, instance_name, self.current_block)
        else:
            if assignable.string():
                value = self.get_string(assignable.string())
            elif assignable.NUMBER():
                value = float(assignable.NUMBER().getText())
            elif assignable.boolean_():
                value = bool(assignable.boolean_().getText())
            else:
                raise NotImplementedError("Only strings and numbers are supported")

            graph_path, existing_data_path, remaining_parts = self.model.find_ref(assignee, self.current_block, return_unfound=True)
            data_path = existing_data_path + remaining_parts
            data = self.model.data[graph_path]
            for p in data_path[:-1]:
                data = data.setdefault(p, {})

            data[data_path[-1]] = value

        return super().visitAssign_stmt(ctx)

    def get_string(self, ctx:AtopileParser.StringContext) -> str:
        return ctx.getText().strip("\"\'")

def build_model(project: Project, path: Path, data_layers: List[Path]=None) -> Model:
    log.info("Building model")
    skip_profiler = log.getEffectiveLevel() > logging.DEBUG

    if data_layers is None:
        data_layers = []

    with profile(profile_log=log, skip=skip_profiler):
        bob = Builder(project)
        model = bob.build(path, data_layers)

    return model
