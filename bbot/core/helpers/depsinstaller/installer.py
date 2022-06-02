import os
import sys
import json
import shutil
import signal
import getpass
import logging
from time import sleep
import subprocess as sp
from itertools import chain
from contextlib import suppress
from ansible_runner.interface import run

from bbot.core.configurator import all_modules_preloaded

log = logging.getLogger("bbot.core.helpers.depsinstaller")


class DepsInstaller:
    def __init__(self, parent_helper):
        self.parent_helper = parent_helper
        self._sudo_password = os.environ.get("BBOT_SUDO_PASS", None)
        self.data_dir = self.parent_helper.cache_dir / "depsinstaller"
        self.setup_status_cache = self.data_dir / "setup_status.json"
        self.command_status = self.data_dir / "command_status"
        self.command_status.mkdir(exist_ok=True, parents=True)
        self.setup_status = self.read_setup_status()
        self.data_dir.mkdir(exist_ok=True, parents=True)

        self.no_deps = self.parent_helper.config.get("no_deps", False)
        self.ansible_debug = self.parent_helper.config.get("debug", False)
        self.force_deps = self.parent_helper.config.get("force_deps", False)
        self.retry_deps = self.parent_helper.config.get("retry_deps", False)
        self.ignore_failed_deps = self.parent_helper.config.get("ignore_failed_deps", False)

    def install(self, *modules):
        succeeded = []
        failed = []
        try:
            notified = False
            for m in modules:
                # assume success if we're ignoring dependencies
                if self.no_deps:
                    succeeded.append(m)
                    continue
                # abort if module name is unknown
                if m not in all_modules_preloaded:
                    log.verbose(f'Module "{m}" not found')
                    failed.append(m)
                    continue
                preloaded = all_modules_preloaded[m]
                # make a hash of the dependencies and check if it's already been handled
                module_hash = self.parent_helper.sha1(str(preloaded["deps"])).hexdigest()
                success = self.setup_status.get(module_hash, None)
                dependencies = list(chain(*preloaded["deps"].values()))
                if len(dependencies) <= 0:
                    log.debug(f'No setup to do for module "{m}"')
                    succeeded.append(m)
                    continue
                else:
                    if success is None or (success is False and self.retry_deps) or self.force_deps:
                        if not notified:
                            log.info(
                                f"Installing dependencies for {len(modules):,} modules. Please be patient, this may take a while."
                            )
                            notified = True
                        log.verbose(f'Installing dependencies for module "{m}"')
                        # get sudo access if we need it
                        if preloaded.get("sudo", False) == True:
                            log.warning(f'Module "{m}" needs root privileges to install its dependencies.')
                            self.ensure_root()
                        success = self.install_module(m)
                        self.setup_status[module_hash] = success
                        if success or self.ignore_failed_deps:
                            log.debug(f'Setup succeeded for module "{m}"')
                            succeeded.append(m)
                        else:
                            log.warning(f'Setup failed for module "{m}"')
                            failed.append(m)
                    else:
                        if success or self.ignore_failed_deps:
                            log.debug(
                                f'Skipping dependency install for module "{m}" because it\'s already done (--force-deps to re-run)'
                            )
                            succeeded.append(m)
                        else:
                            log.warning(
                                f'Skipping dependency install for module "{m}" because it failed previously (--retry-deps to retry or --ignore-failed-deps to ignore)'
                            )
                            failed.append(m)

        finally:
            self.write_setup_status()

        return succeeded, failed

    def install_module(self, module):
        success = True
        preloaded = all_modules_preloaded[module]

        # apt
        deps_apt = preloaded["deps"]["apt"]
        if deps_apt:
            success &= self.apt_install(deps_apt)

        # pip
        deps_pip = preloaded["deps"]["pip"]
        if deps_pip:
            success &= self.pip_install(deps_pip)

        # shell
        deps_shell = preloaded["deps"]["shell"]
        if deps_shell:
            success &= self.shell(module, deps_shell)

        # ansible tasks
        ansible_tasks = preloaded["deps"]["ansible"]
        if ansible_tasks:
            success &= self.tasks(module, ansible_tasks)

        return success

    def pip_install(self, packages):
        packages = ",".join(packages)
        log.verbose(f"Installing the following pip packages: {packages}")
        args = {"name": packages}
        venv = os.environ.get("VIRTUAL_ENV", None)
        if venv is not None:
            args["virtualenv"] = venv
            args["virtualenv_python"] = self.parent_helper.which("python3", "python")
        success, err = self.ansible_run(
            module="pip", args=args, ansible_args={"ansible_python_interpreter": sys.executable}
        )
        if success:
            log.info(f'Successfully installed pip packages "{packages}"')
        else:
            log.warning(f"Failed to install pip packages: {err}")
        return success

    def apt_install(self, packages):
        if not shutil.which("apt"):
            log.warning("apt is not supported on this system. Please manually install the following packages:")
            for p in packages:
                log.warning(f" - {p}")
            return True
        packages = ",".join(packages)
        log.verbose(f"Installing the following apt packages: {packages}")
        args = {"name": packages, "state": "latest", "update_cache": True, "cache_valid_time": 86400}
        success, err = self.ansible_run(
            module="apt",
            args=args,
            ansible_args={
                "ansible_become": True,
                "ansible_become_method": "sudo",
            },
        )
        if success:
            log.info(f'Successfully installed apt packages "{packages}"')
        else:
            log.warning(f"Failed to install apt packages: {err}")
        return success

    def shell(self, module, commands):
        tasks = []
        for i, command in enumerate(commands):
            command_hash = self.parent_helper.sha1(f"{module}_{i}_{command}").hexdigest()
            command_status_file = self.command_status / command_hash
            if type(command) == str:
                command = {"cmd": command}
            command["cmd"] += f" && touch {command_status_file}"
            tasks.append(
                {
                    "name": f"{module}.deps_shell step {i+1}",
                    "ansible.builtin.shell": command,
                    "args": {"executable": "/bin/bash", "creates": str(command_status_file)},
                }
            )
        success, err = self.ansible_run(tasks=tasks)
        if success:
            log.info(f"Successfully ran {len(commands):,} shell commands")
        else:
            log.warning(f"Failed to run shell dependencies")
        return success

    def tasks(self, module, tasks):
        success, err = self.ansible_run(tasks=tasks)
        if success:
            log.info(f"Successfully ran {len(tasks):,} Ansible tasks for {module}")
        else:
            log.warning(f"Failed to run Ansible tasks for {module}")
        return success

    def ansible_run(self, tasks=None, module=None, args=None, ansible_args=None):
        _ansible_args = {"ansible_connection": "local"}
        if ansible_args is not None:
            _ansible_args.update(ansible_args)
        module_args = None
        if args:
            module_args = " ".join([f'{k}="{v}"' for k, v in args.items()])
        log.debug(f"ansible_run(module={module}, args={args}, ansible_args={ansible_args})")
        playbook = None
        if tasks:
            playbook = {"hosts": "all", "tasks": tasks}
            log.debug(json.dumps(playbook, indent=2))
        if self._sudo_password is not None:
            _ansible_args["ansible_become_password"] = self._sudo_password
        playbook_hash = self.parent_helper.sha1(str(playbook)).hexdigest()
        data_dir = self.data_dir / (module if module else f"playbook_{playbook_hash}")
        shutil.rmtree(data_dir, ignore_errors=True)
        data_dir.mkdir(exist_ok=True, parents=True)

        original_sigint_handler = signal.getsignal(signal.SIGINT)
        try:
            res = run(
                playbook=playbook,
                private_data_dir=str(data_dir),
                host_pattern="localhost",
                inventory={
                    "all": {"hosts": {"localhost": _ansible_args}},
                },
                module=module,
                module_args=module_args,
                quiet=not self.ansible_debug,
                verbosity=(3 if self.ansible_debug else 0),
            )
        finally:
            # restore default SIGINT handler
            signal.signal(signal.SIGINT, original_sigint_handler)

        log.debug(f"Ansible status: {res.status}")
        log.debug(f"Ansible return code: {res.rc}")
        success = res.status == "successful"
        err = ""
        for e in res.events:
            if self.ansible_debug and not success:
                log.debug(json.dumps(e, indent=4))
            if e["event"] == "runner_on_failed":
                err = e["event_data"]["res"]["msg"]
                break
        return success, err

    def read_setup_status(self):
        setup_status = dict()
        if self.setup_status_cache.is_file():
            with open(self.setup_status_cache) as f:
                with suppress(Exception):
                    setup_status = json.load(f)
        return setup_status

    def write_setup_status(self):
        with open(self.setup_status_cache, "w") as f:
            json.dump(self.setup_status, f)

    def ensure_root(self):
        if os.geteuid() != 0 and self._sudo_password is None:
            # sleep for a split second to flush previous log messages
            while not self._sudo_password:
                sleep(0.1)
                password = getpass.getpass(prompt="[USER] Please enter sudo password: ")
                if self.verify_sudo_password(password):
                    self._sudo_password = password
                else:
                    log.warning("Incorrect password")

    def verify_sudo_password(self, sudo_pass):
        try:
            sp.run(
                ["sudo", "-S", "-k", "true"],
                input=self.parent_helper.smart_encode(sudo_pass),
                stderr=sp.DEVNULL,
                stdout=sp.DEVNULL,
                check=True,
            )
        except sp.CalledProcessError:
            return False
        return True