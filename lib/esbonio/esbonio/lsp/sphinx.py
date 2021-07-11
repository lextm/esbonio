import hashlib
import logging
import pathlib
import re
import traceback
from typing import Any
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import Tuple

import appdirs
import pygls.uris as uri
from docutils.parsers.rst import Directive
from pydantic import BaseModel
from pydantic import Field
from pygls import IS_WIN
from pygls.lsp.types import Diagnostic
from pygls.lsp.types import DiagnosticSeverity
from pygls.lsp.types import DidSaveTextDocumentParams
from pygls.lsp.types import InitializedParams
from pygls.lsp.types import InitializeParams
from pygls.lsp.types import MessageType
from pygls.lsp.types import Position
from pygls.lsp.types import Range
from sphinx import __version__ as __sphinx_version__
from sphinx.application import Sphinx
from sphinx.domains import Domain
from sphinx.util import console
from sphinx.util.logging import WarningLogRecordTranslator

from .log import LspHandler
from .rst import RstLanguageServer
from .rst import ServerConfig

try:
    from sphinx.util.logging import OnceFilter
except ImportError:
    # OnceFilter is not defined in Sphinx 2.x
    class OnceFilter:
        def filter(self, *args, **kwargs):
            return True


DIAGNOSTIC_SEVERITY = {
    "WARNING": DiagnosticSeverity.Warning,
    "ERROR": DiagnosticSeverity.Error,
}

PATH_VAR_PATTERN = re.compile(r"^\${(\w+)}/?.*")


class MissingConfigError(Exception):
    """Indicates that we couldn't locate the project's 'conf.py'"""


class SphinxConfig(BaseModel):
    """Used to represent either the current Sphinx configuration or the config options
    provided by the user at startup."""

    version: Optional[str]
    """Sphinx's version number."""

    conf_dir: Optional[str] = Field(None, alias="confDir")
    """Can be used to override the default conf.py discovery mechanism."""

    src_dir: Optional[str] = Field(None, alias="srcDir")
    """Can be used to override the default assumption on where the project's rst files
    are located."""

    build_dir: Optional[str] = Field(None, alias="buildDir")
    """Can be used to override the default location for storing build outputs."""

    builder_name: str = Field("html", alias="builderName")
    """The currently used builder name."""


class SphinxServerConfig(ServerConfig):

    hide_sphinx_output: Optional[bool] = Field(False, alias="hideSphinxOutput")
    """A flag to indicate if Sphinx build output should be omitted from the log."""


class InitializationOptions(BaseModel):
    """The initialization options we can expect to receive from a client."""

    sphinx: Optional[SphinxConfig] = Field(default_factory=SphinxConfig)
    """The ``esbonio.sphinx.*`` namespace of options."""

    server: Optional[SphinxServerConfig] = Field(default_factory=SphinxServerConfig)
    """The ``esbonio.server.*`` namespace of options."""


class SphinxLogHandler(LspHandler):
    """A logging handler that can extract errors from Sphinx's build output."""

    def __init__(self, app, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.translator = WarningLogRecordTranslator(app)
        self.only_once = OnceFilter()
        self.diagnostics: Dict[str, List[Diagnostic]] = {}

    def get_location(self, location: str) -> Tuple[str, Optional[int]]:
        path, *parts = location.split(":")
        lineno = None

        # On windows the rest of the path will be the first element of parts
        if IS_WIN:
            path += f":{parts.pop(0)}"

        if len(parts) == 1:
            try:
                lineno = int(parts[0])
            except ValueError:
                pass

        if len(parts) == 2:
            # TODO: There's a possibility that there is an error in a docstring in a
            #       *.py file somewhere. In which case parts would look like
            #       ['docstring of {dotted.name}', '{lineno}']
            #
            #  e.g. ['docstring of esbonio.lsp.sphinx.SphinxLanguageServer.get_domains', '8']
            #
            #       It would be good to handle this case and look up the correct line
            #       number to place a diagnostic.
            pass

        return (uri.from_fs_path(path), lineno)

    def emit(self, record: logging.LogRecord) -> None:

        conditions = [
            "sphinx" not in record.name,
            record.levelno not in {logging.WARNING, logging.ERROR},
            not self.translator,
        ]

        if any(conditions):
            # Log the record as normal
            super().emit(record)
            return

        # Only process errors/warnings once.
        if not self.only_once.filter(record):
            return

        # Let sphinx do what it does to warning/error messages
        self.translator.filter(record)

        doc, lineno = self.get_location(record.location)
        line = lineno or 1
        self.server.logger.debug("Reporting diagnostic at %s:%s", doc, line)

        diagnostic = Diagnostic(
            range=Range(
                start=Position(line=line - 1, character=0),
                end=Position(line=line, character=0),
            ),
            message=record.msg,
            severity=DIAGNOSTIC_SEVERITY.get(
                record.levelno, DiagnosticSeverity.Warning
            ),
        )

        if doc not in self.diagnostics:
            self.diagnostics[doc] = [diagnostic]
        else:
            self.diagnostics[doc].append(diagnostic)

        super().emit(record)


class SphinxLanguageServer(RstLanguageServer):
    """A language server dedicated to working with Sphinx projects."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.app: Optional[Sphinx] = None
        """The Sphinx application instance."""

        self._role_target_types: Optional[Dict[str, List[str]]] = None
        """Cache for role target types."""

        self._role_targets: Optional[Dict[str, List[tuple]]] = None
        """Cache for role target objects."""

    @property
    def configuration(self) -> Dict[str, Any]:
        """Return the server's actual configuration."""
        config = super().configuration

        if self.app:
            app = self.app
            config["sphinx"] = SphinxConfig(
                version=__sphinx_version__,
                confDir=app.confdir,
                srcDir=app.srcdir,
                buildDir=app.outdir,
                builderName=app.builder.name,
            )

        return config

    def initialize(self, params: InitializeParams):
        super().initialize(params)
        self.user_config = InitializationOptions(**params.initialization_options)

        self.app = self._initialize_sphinx()
        self.build()

    def initialized(self, params: InitializedParams):
        # Diagnostics cannot be reported during initialize, so report them now
        self.sync_diagnostics()

    def _initialize_sphinx(self):

        try:
            return self.create_sphinx_app(self.user_config)
        except MissingConfigError:
            self.show_message(
                message="Unable to find your 'conf.py', features that depend on Sphinx will be unavailable",
                msg_type=MessageType.Warning,
            )
        except Exception:
            self.logger.error(traceback.format_exc())
            self.show_message(
                message="Unable to initialize Sphinx, see output window for details.",
                msg_type=MessageType.Error,
            )

    def save(self, params: DidSaveTextDocumentParams):
        super().save(params)

        filepath = uri.to_fs_path(params.text_document.uri)
        if filepath.endswith("conf.py"):
            if self.app:
                conf_dir = pathlib.Path(self.app.confdir)
            else:
                # The user's config is currently broken... where should their conf.py be?
                conf_dir = (
                    find_conf_dir(self.workspace.root_uri, self.user_config) or ""
                )

            if str(conf_dir / "conf.py") == filepath:
                self.app = self._initialize_sphinx()
        else:
            self.clear_diagnostics("sphinx", params.text_document.uri)

        self.build()

    def build(self):
        self.logger.debug("Building...")

        if not self.app:
            return

        # Reset the warnings counter
        self.app._warncount = 0
        self.sphinx_log.diagnostics = {}

        try:
            self.app.build()
        except Exception:
            message = "Unable to build documentation, see output window for details."

            self.logger.error(traceback.format_exc())
            self.show_message(message=message, msg_type=MessageType.Error)

        for doc, diagnostics in self.sphinx_log.diagnostics.items():
            self.logger.debug("Found %d problems for %s", len(diagnostics), doc)
            self.set_diagnostics("sphinx", doc, diagnostics)

        self.send_notification("esbonio/buildComplete", {})
        self.sync_diagnostics()

    def create_sphinx_app(self, options: InitializationOptions) -> Optional[Sphinx]:
        """Create a Sphinx application instance with the given config."""
        sphinx = options.sphinx
        server = options.server

        self.logger.debug("Workspace root %s", self.workspace.root_uri)
        self.logger.debug("Sphinx Config %s", sphinx.dict())

        conf_dir = find_conf_dir(self.workspace.root_uri, sphinx)
        if conf_dir is None:
            raise MissingConfigError()

        builder_name = sphinx.builder_name
        src_dir = get_src_dir(self.workspace.root_uri, conf_dir, sphinx)
        build_dir = get_build_dir(conf_dir, sphinx)
        doctree_dir = build_dir / "doctrees"
        build_dir /= builder_name

        self.logger.debug("Config dir %s", conf_dir)
        self.logger.debug("Src dir %s", src_dir)
        self.logger.debug("Build dir %s", build_dir)
        self.logger.debug("Doctree dir %s", doctree_dir)

        # Disable color escape codes in Sphinx's log messages
        console.nocolor()

        app = Sphinx(
            srcdir=src_dir,
            confdir=conf_dir,
            outdir=build_dir,
            doctreedir=doctree_dir,
            buildername=builder_name,
            status=None,
            warning=None,
            freshenv=True,  # Have Sphinx reload everything on first build.
        )

        # This has to happen after app creation otherwise our handler
        # will get cleared by Sphinx's setup.
        if not server.hide_sphinx_output:
            sphinx_logger = logging.getLogger("sphinx")
            sphinx_logger.setLevel(logging.INFO)

            self.sphinx_log = SphinxLogHandler(app, self)
            self.sphinx_log.setLevel(logging.INFO)

            formatter = logging.Formatter("%(message)s")
            self.sphinx_log.setFormatter(formatter)
            sphinx_logger.addHandler(self.sphinx_log)

        return app

    def get_domains(self) -> Iterator[Tuple[str, Domain]]:
        """Get all the domains registered with an applications.

        Returns a generator that iterates through all of an application's domains,
        taking into account configuration variables such as ``primary_domain``.
        Yielded values will be a tuple of the form ``(prefix, domain)`` where

        - ``prefix`` is the namespace that should be used when referencing items
          in the domain
        - ``domain`` is the domain object itself.

        """

        if self.app is None:
            return []

        domains = self.app.env.domains
        primary_domain = self.app.config.primary_domain

        for name, domain in domains.items():
            prefix = name

            # Items from the standard and primary domains don't require the namespace prefix
            if name == "std" or name == primary_domain:
                prefix = ""

            yield prefix, domain

    def get_directives(self) -> Dict[str, Directive]:
        """Return a dictionary of the known directives"""

        if self._directives is not None:
            return self._directives

        self._directives = super().get_directives()

        for prefix, domain in self.get_domains():
            fmt = "{prefix}:{name}" if prefix else "{name}"

            for name, directive in domain.directives.items():
                key = fmt.format(name=name, prefix=prefix)
                self._directives[key] = directive

        return self._directives

    def get_directive_options(self, name: str):
        """Return the options specification for the given directive."""

        directive = self.get_directives().get(name, None)
        if directive is None:
            return []

        options = directive.option_spec

        if name.startswith("auto") and self.app:
            self.logger.debug("Processing options for '%s' directive", name)
            name = name.replace("auto", "")

            self.logger.debug("Documenter name is '%s'", name)
            documenter = self.app.registry.documenters.get(name, None)

            if documenter is not None:
                options = documenter.option_spec

        return options or []

    def get_roles(self) -> Dict[str, Any]:
        """Return a dictionary of known roles."""

        if self._roles is not None:
            return self._roles

        self._roles = super().get_roles()

        for prefix, domain in self.get_domains():
            fmt = "{prefix}:{name}" if prefix else "{name}"

            for name, role in domain.roles.items():
                key = fmt.format(name=name, prefix=prefix)
                self._roles[key] = role

        return self._roles

    def get_role_target_types(self, name: str, domain: str = "") -> List[str]:
        """Return a map indicating which object types a role is capable of linking
        with.

        For example

        .. code-block:: python

           {
               "func": ["py:function"],
               "class": ["py:class", "py:exception"]
           }
        """

        key = f"{domain}:{name}" if domain else name

        if self._role_target_types is not None:
            return self._role_target_types.get(key, [])

        self._role_target_types = {}

        for prefix, domain in self.get_domains():
            fmt = "{prefix}:{name}" if prefix else "{name}"

            for name, item_type in domain.object_types.items():
                for role in item_type.roles:
                    role_key = fmt.format(name=role, prefix=prefix)
                    target_types = self._role_target_types.get(role_key, list())
                    target_types.append(fmt.format(name=name, prefix=prefix))

                    self._role_target_types[role_key] = target_types

        return self._role_target_types.get(key, [])

    def get_role_targets(self, name: str, domain: str = "") -> List[tuple]:
        """Return a list of objects targeted by the given role.

        Parameters
        ----------
        name:
           The name of the role
        domain:
           The domain the role is a part of, if applicable.
        """

        if self._role_targets is None:
            self._index_role_targets()

        targets = []
        for target_type in self.get_role_target_types(name, domain):
            targets += self._role_targets.get(target_type, [])

        return targets

    def _index_role_targets(self):
        self._role_targets = {}

        for prefix, domain_obj in self.get_domains():
            fmt = "{prefix}:{name}" if prefix else "{name}"

            for obj in domain_obj.get_objects():
                obj_key = fmt.format(name=obj[2], prefix=prefix)
                objects = self._role_targets.get(obj_key, list())
                objects.append(obj)

                self._role_targets[obj_key] = objects

    def get_intersphinx_projects(self) -> List[str]:
        """Return the list of configured intersphinx project names."""

        inv = getattr(self.app.env, "intersphinx_named_inventory", {})
        return list(inv.keys())

    def get_intersphinx_targets(
        self, project: str, name: str, domain: str = ""
    ) -> Dict[str, Dict[str, tuple]]:
        """Return the intersphinx objects targeted by the given role.

        Parameters
        ----------
        project:
           The project to return targets from
        name:
           The name of the role
        domain:
           The domain the role is a part of, if applicable.
        """

        inv = getattr(self.app.env, "intersphinx_named_inventory", {})
        if project not in inv:
            return {}

        targets = {}
        inv = inv[project]
        primary_domain = self.app.config.primary_domain or ""

        for target_type in self.get_role_target_types(name, domain):

            if target_type in inv:
                targets[target_type] = inv[target_type]
                continue

            # Intersphinx targets are always namespaced, so we would need to be explicit
            # about the domain the type sits in.
            if f"{primary_domain}:{target_type}" in inv:
                targets[target_type] = inv[f"{primary_domain}:{target_type}"]
                continue

            # The 'std' domain must also be considered.
            if f"std:{target_type}" in inv:
                targets[target_type] = inv[f"std:{target_type}"]

        return targets


def find_conf_dir(root_uri: str, config: SphinxConfig) -> Optional[pathlib.Path]:
    """Attempt to find Sphinx's configuration file within the given workspace."""

    root = pathlib.Path(uri.to_fs_path(root_uri))

    if config.conf_dir:
        return expand_conf_dir(root, config.conf_dir)

    ignore_paths = [".tox", "site-packages"]

    for candidate in root.glob("**/conf.py"):
        # Skip any files that obviously aren't part of the project
        if any(path in str(candidate) for path in ignore_paths):
            continue

        return candidate.parent


def expand_conf_dir(root_dir: str, conf_dir: str) -> str:
    """Expand the user provided conf_dir into a real path.

    Here is where we handle "variables" that can be included in the path, currently
    we support

    - ``${workspaceRoot}`` which expands to the workspace root as provided by the
      language client.

    Parameters
    ----------
    root_dir:
       The workspace root path
    conf_dir:
       The user provided path
    """

    match = PATH_VAR_PATTERN.match(conf_dir)
    if not match or match.group(1) != "workspaceRoot":
        return conf_dir

    conf = pathlib.Path(conf_dir).parts[1:]
    return pathlib.Path(root_dir, *conf).resolve()


def get_src_dir(
    root_uri: str, conf_dir: pathlib.Path, config: SphinxConfig
) -> pathlib.Path:
    """Get the src dir to use based on the given conifg.

    By default the src dir will be the same as the conf dir, but this can
    be overriden by the given conifg.

    There are a number of "variables" that can be included in the path,
    currently we support

    - ``${workspaceRoot}`` which expands to the workspace root as provided
      by the language client.
    - ``${confDir}`` which expands to the configured config dir.

    Parameters
    ----------
    root_uri:
       The workspace root uri
    conf_dir:
       The project's conf dir
    config:
       The user's configuration.
    """

    if not config.src_dir:
        return conf_dir

    src_dir = config.src_dir
    root_dir = uri.to_fs_path(root_uri)

    match = PATH_VAR_PATTERN.match(src_dir)
    if match and match.group(1) == "workspaceRoot":
        src = pathlib.Path(src_dir).parts[1:]
        return pathlib.Path(root_dir, *src).resolve()

    if match and match.group(1) == "confDir":
        src = pathlib.Path(src_dir).parts[1:]
        return pathlib.Path(conf_dir, *src).resolve()

    return src_dir


def get_build_dir(conf_dir: pathlib.Path, config: SphinxConfig) -> pathlib.Path:

    if config.build_dir is None:
        # Try to pick a sensible dir based on the project's location
        cache = appdirs.user_cache_dir("esbonio", "swyddfa")
        project = hashlib.md5(str(conf_dir).encode()).hexdigest()

        return pathlib.Path(cache) / project

    # Convert path to/from uri so that any path quirks from windows are
    # automatically handled
    build_uri = uri.from_fs_path(config.build_dir)
    return pathlib.Path(uri.to_fs_path(build_uri))
