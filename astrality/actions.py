"""
Module defining class-representation of module actions.

Each action class type encapsulates the user specified options available for
that specific action type. The action itself can be performed by invoking the
object method `execute()`.

One of the main goals with Action, is that the arity of execute is 0.
This means that we unfortunately need to pass a reference to global mutable
state, i.e. the context store.

Another goal is that none of the subclasses require the global configuration
of the entire application, just the action configuration itself. Earlier
implementations required GlobalApplicationConfig to be passed arround in the
entire run-stack, which was quite combersome. Some of the limitations with this
approach could be solved if we implement GlobalApplicationConfig as a singleton
which could be imported and accessed independently from other modules.
"""

import abc
from collections import defaultdict
import logging
from os.path import relpath
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import (
    Any,
    Callable,
    DefaultDict,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

from mypy_extensions import TypedDict

from astrality import compiler, utils
from astrality.config import expand_path, insert_into

Replacer = Callable[[str], str]


class Action(abc.ABC):
    """
    Superclass for module action types.

    :param options: A dictionary containing the user options for a given module
        action type.
    :param directory: The directory used as anchor for relative paths. This
        must be an absolute path.
    :param replacer: Placeholder substitutor of string user options.
    :param context_store: A reference to the global context store.
    """

    directory: Path
    priority: int

    def __init__(
        self,
        options: Union['ImportContextDict', 'CompileDict', 'RunDict'],
        directory: Path,
        replacer: Replacer,
        context_store: compiler.Context,
    ) -> None:
        """Contstruct action object."""
        # If no options are provided, use null object pattern
        self.null_object = not bool(options)

        assert directory.is_absolute()
        self.directory = directory
        self._options = options
        self._replace = replacer
        self.context_store = context_store

    def replace(self, string: str) -> str:
        """
        Return converted string, substitution defined by `replacer`.

        This is used to replace placeholders such as {event}.
        This redirection is necessary due to python/mypy/issues/2427

        :param string: String configuration option.
        :return: String with placeholders substituted.
        """
        return self._replace(string)

    def option(self, key: str, path: bool = False) -> Any:
        """
        Return user specified action option.

        All option value access should go through this helper function, as
        it replaces relevant placeholders users might have specified.

        :param key: The key of the user option that should be retrieved.
        :param path: If True, convert string path to Path.is_absolute().
        :return: Processed action configuration value.
        """
        option_value = self._options.get(key)

        if option_value is None:
            return None
        elif path:
            # The option value represents a path, that should be converted
            # to an absolute pathlib.Path object
            assert isinstance(option_value, str)
            substituted_string_path = self.replace(option_value)
            return self._absolute_path(of=substituted_string_path)
        elif isinstance(option_value, str):
            # The option is a string, and any placeholders should be
            # substituted before it is returned.
            return self.replace(option_value)
        else:
            return option_value

    def _absolute_path(self, of: str) -> Path:
        """
        Return absolute path from relative string path.

        :param of: Relative path.
        :return: Absolute path anchored to `self.directory`.
        """
        return expand_path(
            path=Path(of),
            config_directory=self.directory,
        )

    @abc.abstractmethod
    def execute(self) -> Any:
        """Execute defined action."""

    def __repr__(self) -> str:
        """Return string representation of Action object."""
        return self.__class__.__name__ + f'({self._options})'


class RequiredImportContextDict(TypedDict):
    """Required fields of a import_context action."""

    from_path: str


class ImportContextDict(RequiredImportContextDict, total=False):
    """Allowable fields of an import_context action."""

    from_section: str
    to_section: str


class ImportContextAction(Action):
    """
    Import context into global context store.

    :param context_store: A mutable reference to the global context store.

    See :class:`Action` for documentation for the other parameters.
    """

    priority = 100
    context_store: compiler.Context

    def execute(self) -> None:
        """Import context section(s) according to user configuration block."""
        if self.null_object:
            # Null object does nothing
            return None

        insert_into(  # type: ignore
            context=self.context_store,
            from_config_file=self.option(key='from_path', path=True),
            section=self.option(key='to_section'),
            from_section=self.option(key='from_section'),
        )


class RequiredCompileDict(TypedDict):
    """Required fields of compile action."""

    source: str


class CompileDict(RequiredCompileDict, total=False):
    """Allowable fields of compile action."""

    target: str
    permissions: str


class CompileAction(Action):
    """Compile template action."""

    priority = 200

    def __init__(self, *args, **kwargs) -> None:
        """Construct compile action object."""
        super().__init__(*args, **kwargs)
        self._performed_compilations: DefaultDict[Path, Set[Path]] = \
            defaultdict(set)

    def execute(self) -> Dict[Path, Path]:
        """
        Compile template to target destination.

        :return: Dictionary with template keys and compile target values.
        """
        if self.null_object:
            # Null objects do nothing
            return {}
        elif 'target' not in self._options:
            # If no target is specified, then we can create a temporary file
            # and insert it into the configuration options.
            template = self.option(key='source', path=True)
            target = self._create_temp_file(template.name)
            self._options['target'] = str(target)  # type: ignore

        template_source = self.option(key='source', path=True)
        target = self.option(key='target', path=True)

        compilations: Dict[Path, Path] = {}
        if template_source.is_file():
            # Single template file, so straight forward compilation
            self.compile_template(template=template_source, target=target)
            self._performed_compilations[template_source].add(target)
            compilations = {template_source: target}

        elif template_source.is_dir():
            # The template source is a directory, so we will recurse over
            # all the files and compile every single file while preserving
            # the directory hierarchy
            templates = tuple(
                path
                for path
                in template_source.glob('**/*')
                if path.is_file()
            )
            targets = tuple(
                target / relpath(template_file, start=template_source)
                for template_file
                in templates
            )

            for template, target in zip(templates, targets):
                self.compile_template(template=template, target=target)
                self._performed_compilations[template].add(target)
                compilations[template] = target

        else:
            logger = logging.getLogger(__name__)
            logger.error(
                f'Could not compile template "{template_source}" '
                f'to target "{target}". No such path!',
            )

        return compilations

    def compile_template(self, template: Path, target: Path) -> None:
        compiler.compile_template(
            template=template,
            target=target,
            context=self.context_store,
            shell_command_working_directory=self.directory,
            permissions=self.option(key='permissions'),
        )

    def performed_compilations(self) -> DefaultDict[Path, Set[Path]]:
        """
        Return dictionary containing all performed compilations.

        :return: Dictinary with keys containing compiled templates, and values
            as a set of target paths.
        """
        return self._performed_compilations

    def _create_temp_file(self, name) -> Path:
        """
        Create persisted tempory file.

        :return: Path object pointing to the created temporary file.
        """
        temp_file = NamedTemporaryFile(  # type: ignore
            prefix=name + '-',
            # dir=Path(self.temp_directory),
        )

        # NB: These temporary files need to be persisted during the entirity of
        # the scripts runtime, since the files are deleted when they go out of
        # scope.
        if not hasattr(self, 'temp_files'):
            self.temp_files = [temp_file]
        else:
            self.temp_files.append(temp_file)

        return Path(temp_file.name)

    def __contains__(self, other) -> bool:
        """Return True if run action is responsible for template."""
        assert other.is_absolute()

        if not self.option(key='source', path=True) == other:
            # This is not a managed template, so we will not recompile
            return False

        # Return True if the template has been compiled
        return other in self.performed_compilations()


class RunDict(TypedDict):
    """Required fields of run action user config."""

    shell: str
    timeout: Union[int, float]


class RunAction(Action):
    """Run shell command Action sub-class."""

    priority = 300

    def execute(
        self,
        default_timeout: Union[int, float] = 0,
    ) -> Optional[Tuple[str, str]]:
        """
        Execute shell command action.

        :param default_timeout: Run timeout in seconds if no specific value is
            specified in `options`.
        :return: 2-tuple containing the executed command and its resulting
            stdout.
        """
        if self.null_object:
            # Null objects do nothing
            return None

        command = self.option(key='shell')
        timeout = self.option(key='timeout')

        logger = logging.getLogger(__name__)
        logger.info(f'Running command "{command}".')

        result = utils.run_shell(
            command=command,
            timeout=timeout or default_timeout,
            working_directory=self.directory,
        )
        return command, result


class TriggerDictRequired(TypedDict):
    """Required fields of a trigger module action."""

    block: str


class TriggerDict(TriggerDictRequired, total=False):
    """Optional fields of a trigger module action."""

    path: str


class Trigger:
    """
    A class representing an instruction to trigger a specific action block.

    :ivar block: The block to be trigger, for example 'on_startup',
        'on_event', 'on_exit', or 'on_modified'.
    :ivar specified_path: The string path specified for a 'on_modified' block.
    :ivar relative_path: The relative pathlib.Path specified by
        `specified_path`.
    :ivar absolute_path: The absolute path specified by `specified_path`.
    """

    block: str
    specified_path: Optional[str]
    relative_path: Optional[Path]
    absolute_path: Optional[Path]

    def __init__(
        self,
        block: str,
        specified_path: Optional[str] = None,
        relative_path: Optional[Path] = None,
        absolute_path: Optional[Path] = None,
    ) -> None:
        """Construct trigger instruction."""
        self.block = block
        self.specified_path = specified_path
        self.relative_path = relative_path
        self.absolute_path = absolute_path


class TriggerAction(Action):
    """Action sub-class representing a trigger action."""

    priority = 0

    def execute(self) -> Optional[Trigger]:
        """
        Return trigger instruction.

        If no trigger is specified, return None.

        :return: Optional :class:`.Trigger` instance.
        """
        if self.null_object:
            """Null objects do nothing."""
            return None

        block = self.option(key='block')

        if block != 'on_modified':
            # We do not need any paths, as the trigger block is not relative to
            # any modified path.
            return Trigger(block=block)

        # The modified path specified by the user configuration
        specified_path = self.option(key='path')

        # Instantiate relative and absolute pathlib.Path objects
        relative_path = Path(specified_path)
        absolute_path = self._absolute_path(of=specified_path)

        # Return 'on_modified' Trigger object with path information
        return Trigger(
            block=block,
            specified_path=specified_path,
            relative_path=relative_path,
            absolute_path=absolute_path,
        )


class ActionBlockDict(TypedDict, total=False):
    """Valid keys in an action block."""

    import_context: Union[ImportContextDict, List[ImportContextDict]]
    compile: Union[CompileDict, List[CompileDict]]
    run: Union[RunDict, List[RunDict]]
    trigger: Union[TriggerDict, List[TriggerDict]]


class ActionBlock:
    """
    Class representing a module action block, e.g. 'on_startup'.

    :param action_block: Dictinary containing all actions to be performed.
    :param directory: The directory used as anchor for relative paths. This
        must be an absolute path.
    :param replacer: Placeholder substitutor of string user options.
    :param context_store: A reference to the global context store.
    """

    _import_context_actions: List[ImportContextAction]
    _compile_actions: List[CompileAction]
    _run_actions: List[RunAction]
    _trigger_actions: List[TriggerAction]

    def __init__(
        self,
        action_block: ActionBlockDict,
        directory: Path,
        replacer: Replacer,
        context_store: compiler.Context,
    ) -> None:
        """
        Construct ActionBlock object.

        Instantiates action types and appends to:
        self._run_actions: List[RunAction], and so on...
        """
        assert directory.is_absolute()
        self.action_block = action_block

        for identifier, action_type in (
            ('import_context', ImportContextAction),
            ('compile', CompileAction),
            ('run', RunAction),
            ('trigger', TriggerAction),
        ):
            # Create and persist a list of all ImportContextAction objects
            action_configs = utils.cast_to_list(  # type: ignore
                self.action_block.get(identifier, {}),  # type: ignore
            )
            setattr(
                self,
                f'_{identifier}_actions',
                [action_type(  # type: ignore
                    options=action_config,
                    directory=directory,
                    replacer=replacer,
                    context_store=context_store,
                ) for action_config in action_configs
                ],
            )

    def import_context(self) -> None:
        """Import context into global context store."""
        for import_context_action in self._import_context_actions:
            import_context_action.execute()

    def compile(self) -> None:
        """Compile all templates."""
        for compile_action in self._compile_actions:
            compile_action.execute()

    def run(
        self,
        default_timeout: Union[int, float],
    ) -> Tuple[Tuple[str, str], ...]:
        """
        Run shell commands.

        :param default_timeout: How long to wait for run commands to exit
        :return: Tuple of 2-tuples containing (shell_command, stdout,)
        """
        results: Tuple[Tuple[str, str], ...] = tuple()
        for run_action in self._run_actions:
            result = run_action.execute(
                default_timeout=default_timeout,
            )
            if result:
                # Run action is not null object, so we can return results
                command, stdout = result
                results += ((command, stdout,),)

        return results

    def triggers(self) -> Tuple[Trigger, ...]:
        """
        Return all trigger instructions specified in action block.

        :return: Tuple of Trigger objects specified in action block.
        """
        return tuple(
            trigger_action.execute()  # type: ignore
            for trigger_action
            in self._trigger_actions
            if not trigger_action.null_object
        )

    def execute(self, default_timeout: Union[int, float]) -> None:
        """
        Execute all actions in action block.

        The order of execution is:
            1) Perform all context imports into the context store.
            2) Compile all templates.
            3) Run all shell commands.
        """
        self.import_context()
        self.compile()
        self.run(default_timeout=default_timeout)

    def performed_compilations(self) -> DefaultDict[Path, Set[Path]]:
        """
        Return all earlier performed compilations.

        :return: Dictionary with template keys and target path set.
        """
        all_compilations: DefaultDict[Path, Set[Path]] = defaultdict(set)
        for compile_action in self._compile_actions:
            compilations = compile_action.performed_compilations()
            for template, targets in compilations.items():
                all_compilations[template] |= targets

        return all_compilations
