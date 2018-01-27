"""Module implementing user configured custom functionality."""

import logging
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile
from typing import Dict, Optional, Union

from compiler import compile_template
from resolver import Resolver
from timer import TIMERS

logger = logging.getLogger('astrality')


class Module:
    """
    Class for executing user defined functionality in [module/*] section.

    The module can define a set of commands to be run on astrality startup and
    exit, in addition to every time a given type of period changes.

    Available module options are as follows:
    - enabled: true or false. Defaults to true.
    - timer: Name of timer which determines when to run module commands.
    - template_files: Space-seperated paths of templates to be compiled.


    Commands are run in the users shell, and can use the following placeholders:
    - {period}: The period specified by the timer instance.
    - {compiled_template}: The path to the compiled template specified in the
                           'template' option.
    """

    def __init__(
        self,
        module_config: Union[dict, Resolver],
        application_config: Union[dict, Resolver],
    ) -> None:
        """
        Initialize Module object with a section from a config dictionary.

        Section name must be [module/*], where * is the module name.
        In addition, the enabled option must be set to "true", or not set at
        all.
        """
        self.application_config = application_config

        section = next(iter(module_config.keys()))
        self.name = section.split('/')[1]

        self.config = module_config[section]
        self.timer = TIMERS[self.config['timer']](application_config)  # type: ignore

        # Commands to run at specified times
        self.startup_command = self.config.get('on_startup')
        self.period_change_command = self.config.get('on_period_change')
        self.exit_command = self.config.get('on_exit')

        # Find and prepare templates and compiation targets
        self._prepare_templates()
        self._prepare_compiled_templates()

    def _prepare_templates(self) -> None:
        """Determine template sources and compilation targets."""

        self.template_file: Optional[Path] = self.config.get('template_file')

        if self.template_file:
            self.template_file = self.expand_path(Path(self.template_file))

            if not self.template_file.is_file():
                logger.error(\
                    f'[module/{self.name}] Template file "{self.template_file}"'
                    ' does not exist. Skipping compilation of this file.'
                )
                self.template_file = None

    def _prepare_compiled_templates(self) -> None:
        """Find compilation targets, and possibly create temporary target files."""

        self.compiled_template: Optional[Path] = None
        if not self.template_file:
            # We do not need a compilation target if no template has been set.
            return

        self.compiled_template: Optional[Path] = self.config.get('compiled_template')
        if self.compiled_template:
            self.compiled_template = self.expand_path(Path(self.compiled_template))
        else:
            self.compiled_template = self.create_temp_file()

    def create_temp_file(self) -> Path:
        """Create a temp file used as a compilation target, returning its path."""

        # NB: These temporary files/directories need to be persisted during the
        # entirity of the scripts runtime, since the files are deleted when they
        # go out of scope

        self.temp_file = NamedTemporaryFile(  # type: ignore
            prefix=self.name + '-',
            dir=self.application_config['_runtime']['temp_directory'],
        )

        return Path(self.temp_file.name)

    def expand_path(self, path: Path) -> Path:
        """
        Return an absolute path from a (possibly) relative path.

        Relative paths are relative to $ASTRALITY_CONFIG_HOME, and ~ is
        expanded to the home directory of $USER.
        """

        path = Path.expanduser(path)

        if not path.is_absolute():
            path = Path(
                self.application_config['_runtime']['config_directory'],
                path,
            )

        return path

    def startup(self) -> None:
        """Commands to be run on Module instance startup."""

        if self.startup_command:
            logger.info(f'[module/{self.name}] Running startup command.')
            self.run_shell(command=self.startup_command)
        else:
            logger.debug(f'[module/{self.name}] No startup command specified.')

    def period_change(self) -> None:
        """Commands to be run when self.timer period changes."""

        if self.period_change_command:
            logger.info(f'[module/{self.name}] Running period change command.')
            self.run_shell(command=self.period_change_command)
        else:
            logger.debug(f'[module/{self.name}] No period change command specified.')

    def exit(self) -> None:
        """Commands to be run on Module instance shutdown."""

        if self.exit_command:
            logger.info(f'[module/{self.name}] Running exit command.')
            self.run_shell(command=self.exit_command)
        else:
            logger.debug(f'[module/{self.name}] No exit command specified.')

        if hasattr(self, 'temp_file'):
            # A temporary file has been created for this module and it should
            # be deleted.
            self.temp_file.close()

    def compile_template(self) -> None:
        pass

    def run_shell(self, command) -> None:
        command = command.format(
            period=self.timer.period(),
            compiled_template=self.compiled_template,
        )
        logger.info(f'[module/{self.name}] Running command "{command}".')
        process = subprocess.Popen(
            command,
            cwd=self.application_config['_runtime']['config_directory'],
            shell=True,
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            process.wait(timeout=2)

            for line in process.stdout:
                logger.info(str(line))

            for error_line in process.stdout:
                logger.error(str(error_line))

            if process.returncode != 0:
                logger.error(
                    f'Command "{command}" exited with non-zero return code: ' +
                    str(process.returncode)
                )

        except subprocess.TimeoutExpired:
            logger.warning(
                f'The command "{command}" used more than 2 seconds in order to'
                'finish. The exit code can not be verified. This might be '
                'intentional for background processes and daemons.'
            )

    @staticmethod
    def valid_class_section(section: Dict[str, Dict[str, str]]) -> bool:
        if not len(section) == 1:
            raise RuntimeError(
                'Tried to check module section with dict '
                'which does not have exactly one item.',
            )

        try:
            module_name = next(iter(section.keys()))
            valid_module_name = module_name.split('/')[0] == 'module'
            enabled = section[module_name]['enabled'].lower() == 'true'
            return valid_module_name and enabled
        except KeyError:
            return False
