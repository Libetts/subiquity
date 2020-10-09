# Copyright 2015 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import datetime
import logging
import os
import re
import shutil
import sys
import tempfile
import traceback

from curtin.commands.install import (
    ERROR_TARFILE,
    INSTALL_LOG,
    )
from curtin.util import write_file

from systemd import journal

import yaml

from subiquitycore.async_helpers import (
    run_in_thread,
    schedule_task,
    )
from subiquitycore.context import Status, with_context
from subiquitycore.utils import (
    arun_command,
    astart_command,
    )

from subiquity.common.errorreport import ErrorReportKind
from subiquity.common.types import InstallState
from subiquity.controller import SubiquityTuiController
from subiquity.journald import journald_listen
from subiquity.ui.views.installprogress import ProgressView


log = logging.getLogger("subiquitycore.controller.installprogress")


class TracebackExtractor:

    start_marker = re.compile(r"^Traceback \(most recent call last\):")
    end_marker = re.compile(r"\S")

    def __init__(self):
        self.traceback = []
        self.in_traceback = False

    def feed(self, line):
        if not self.traceback and self.start_marker.match(line):
            self.in_traceback = True
        elif self.in_traceback and self.end_marker.match(line):
            self.traceback.append(line)
            self.in_traceback = False
        if self.in_traceback:
            self.traceback.append(line)


class InstallProgressController(SubiquityTuiController):

    def __init__(self, app):
        super().__init__(app)
        self.model = app.base_model
        self.progress_view = ProgressView(self)
        self.crash_report_ref = None
        self._install_state = InstallState.NOT_STARTED

        self.reboot_clicked = asyncio.Event()
        if self.answers.get('reboot', False):
            self.reboot_clicked.set()

        self.unattended_upgrades_proc = None
        self.unattended_upgrades_ctx = None
        self._event_syslog_id = 'curtin_event.%s' % (os.getpid(),)
        self.tb_extractor = TracebackExtractor()
        self.curtin_event_contexts = {}

    def event(self, event):
        if event["SUBIQUITY_EVENT_TYPE"] == "start":
            self.progress_view.event_start(
                event["SUBIQUITY_CONTEXT_ID"],
                event.get("SUBIQUITY_CONTEXT_PARENT_ID"),
                event["MESSAGE"])
        elif event["SUBIQUITY_EVENT_TYPE"] == "finish":
            self.progress_view.event_finish(
                event["SUBIQUITY_CONTEXT_ID"])

    def log_line(self, event):
        log_line = event['MESSAGE']
        self.progress_view.add_log_line(log_line)

    def interactive(self):
        return self.app.interactive()

    def start(self):
        self.install_task = schedule_task(self.install())

    @with_context()
    async def apply_autoinstall_config(self, context):
        await self.install_task
        self.app.reboot_on_exit = True

    @property
    def install_state(self):
        return self._install_state

    def update_state(self, state):
        self._install_state = state
        self.progress_view.update_for_state(state)

    def tpath(self, *path):
        return os.path.join(self.model.target, *path)

    def curtin_error(self):
        kw = {}
        if sys.exc_info()[0] is not None:
            log.exception("curtin_error")
            self.progress_view.add_log_line(traceback.format_exc())
        if self.tb_extractor.traceback:
            kw["Traceback"] = "\n".join(self.tb_extractor.traceback)
        crash_report = self.app.make_apport_report(
            ErrorReportKind.INSTALL_FAIL, "install failed", interrupt=False,
            **kw)
        if crash_report is not None:
            self.crash_report_ref = crash_report.ref()
        self.progress_view.finish_all()
        self.progress_view.set_status(
            ('info_error', _("An error has occurred")))
        if not self.showing:
            self.app.controllers.index = self.controller_index - 1
            self.app.next_screen()
        self.update_state(InstallState.ERROR)
        if self.crash_report_ref is not None:
            self.app.show_error_report(self.crash_report_ref)

    def logged_command(self, cmd):
        return ['systemd-cat', '--level-prefix=false',
                '--identifier=' + self.app.log_syslog_id] + cmd

    def log_event(self, event):
        self.curtin_log(event)

    def curtin_event(self, event):
        e = {
            "EVENT_TYPE": "???",
            "MESSAGE": "???",
            "NAME": "???",
            "RESULT": "???",
            }
        prefix = "CURTIN_"
        for k, v in event.items():
            if k.startswith(prefix):
                e[k[len(prefix):]] = v
        event_type = e["EVENT_TYPE"]
        if event_type == 'start':
            def p(name):
                parts = name.split('/')
                for i in range(len(parts), -1, -1):
                    yield '/'.join(parts[:i]), '/'.join(parts[i:])

            curtin_ctx = None
            for pre, post in p(e["NAME"]):
                if pre in self.curtin_event_contexts:
                    parent = self.curtin_event_contexts[pre]
                    curtin_ctx = parent.child(post, e["MESSAGE"])
                    self.curtin_event_contexts[e["NAME"]] = curtin_ctx
                    break
            if curtin_ctx:
                curtin_ctx.enter()
        if event_type == 'finish':
            status = getattr(Status, e["RESULT"], Status.WARN)
            curtin_ctx = self.curtin_event_contexts.pop(e["NAME"], None)
            if curtin_ctx is not None:
                curtin_ctx.exit(result=status)

    def curtin_log(self, event):
        self.tb_extractor.feed(event['MESSAGE'])

    def _write_config(self, path, config):
        with open(path, 'w') as conf:
            datestr = '# Autogenerated by SUbiquity: {} UTC\n'.format(
                str(datetime.datetime.utcnow()))
            conf.write(datestr)
            conf.write(yaml.dump(config))

    def _get_curtin_command(self):
        config_file_name = 'subiquity-curtin-install.conf'

        if self.opts.dry_run:
            config_location = os.path.join('.subiquity/', config_file_name)
            log_location = '.subiquity/install.log'
            event_file = "examples/curtin-events.json"
            if 'install-fail' in self.app.debug_flags:
                event_file = "examples/curtin-events-fail.json"
            curtin_cmd = [
                "python3", "scripts/replay-curtin-log.py", event_file,
                self._event_syslog_id, log_location,
                ]
        else:
            config_location = os.path.join('/var/log/installer',
                                           config_file_name)
            curtin_cmd = [sys.executable, '-m', 'curtin', '--showtrace', '-c',
                          config_location, 'install']
            log_location = INSTALL_LOG

        self._write_config(
            config_location,
            self.model.render(syslog_identifier=self._event_syslog_id))

        self.app.note_file_for_apport("CurtinConfig", config_location)
        self.app.note_file_for_apport("CurtinLog", log_location)
        self.app.note_file_for_apport("CurtinErrors", ERROR_TARFILE)

        return curtin_cmd

    @with_context(description="umounting /target dir")
    async def unmount_target(self, *, context, target):
        cmd = [
            sys.executable, '-m', 'curtin', 'unmount',
            '-t', target,
            ]
        if self.opts.dry_run:
            cmd = ['sleep', str(0.2/self.app.scale_factor)]
        await arun_command(cmd)
        if not self.opts.dry_run:
            shutil.rmtree(target)

    @with_context(
        description="installing system", level="INFO", childlevel="DEBUG")
    async def curtin_install(self, *, context):
        log.debug('curtin_install')
        self.curtin_event_contexts[''] = context

        loop = self.app.aio_loop

        fds = [
            journald_listen(loop, [self.app.log_syslog_id], self.curtin_log),
            journald_listen(loop, [self._event_syslog_id], self.curtin_event),
            ]

        curtin_cmd = self._get_curtin_command()

        log.debug('curtin install cmd: {}'.format(curtin_cmd))

        async with self.app.install_lock_file.exclusive():
            try:
                our_tty = os.ttyname(0)
            except OSError:
                # This is a gross hack for testing in travis.
                our_tty = "/dev/not a tty"
            self.app.install_lock_file.write_content(our_tty)
            journal.send("starting install", SYSLOG_IDENTIFIER="subiquity")
            try:
                cp = await arun_command(
                    self.logged_command(curtin_cmd), check=True)
            finally:
                for fd in fds:
                    loop.remove_reader(fd)

        log.debug('curtin_install completed: %s', cp.returncode)

    def cancel(self):
        pass

    @with_context()
    async def install(self, *, context):
        context.set('is-install-context', True)
        try:
            await asyncio.wait(
                {e.wait() for e in self.model.install_events})

            self.update_state(InstallState.NEEDS_CONFIRMATION)

            await self.model.confirmation.wait()

            self.update_state(InstallState.RUNNING)

            if os.path.exists(self.model.target):
                await self.unmount_target(
                    context=context, target=self.model.target)

            await self.curtin_install(context=context)

            self.update_state(InstallState.POST_WAIT)

            await asyncio.wait(
                {e.wait() for e in self.model.postinstall_events})

            await self.drain_curtin_events(context=context)

            self.update_state(InstallState.POST_RUNNING)

            await self.postinstall(context=context)

            if self.model.network.has_network:
                self.update_state(InstallState.UU_RUNNING)
                await self.run_unattended_upgrades(context=context)

            self.update_state(InstallState.DONE)
        except Exception:
            self.curtin_error()
            if not self.interactive():
                raise

    async def move_on(self):
        await self.install_task
        self.app.next_screen()

    async def drain_curtin_events(self, *, context):
        waited = 0.0
        while self.progress_view.ongoing and waited < 5.0:
            await asyncio.sleep(0.1)
            waited += 0.1
        log.debug("waited %s seconds for events to drain", waited)
        self.curtin_event_contexts.pop('', None)

    @with_context(
        description="final system configuration", level="INFO",
        childlevel="DEBUG")
    async def postinstall(self, *, context):
        autoinstall_path = os.path.join(
            self.app.root, 'var/log/installer/autoinstall-user-data')
        autoinstall_config = "#cloud-config\n" + yaml.dump(
            {"autoinstall": self.app.make_autoinstall()})
        write_file(autoinstall_path, autoinstall_config, mode=0o600)
        await self.configure_cloud_init(context=context)
        packages = []
        if self.model.ssh.install_server:
            packages = ['openssh-server']
        packages.extend(self.app.base_model.packages)
        for package in packages:
            await self.install_package(context=context, package=package)
        await self.restore_apt_config(context=context)

    @with_context(description="configuring cloud-init")
    async def configure_cloud_init(self, context):
        await run_in_thread(self.model.configure_cloud_init)

    @with_context(
        name="install_{package}",
        description="installing {package}")
    async def install_package(self, *, context, package):
        if self.opts.dry_run:
            cmd = ["sleep", str(2/self.app.scale_factor)]
        else:
            cmd = [
                sys.executable, "-m", "curtin", "system-install", "-t",
                "/target",
                "--", package,
                ]
        await arun_command(self.logged_command(cmd), check=True)

    @with_context(description="restoring apt configuration")
    async def restore_apt_config(self, context):
        if self.opts.dry_run:
            cmds = [["sleep", str(1/self.app.scale_factor)]]
        else:
            cmds = [
                ["umount", self.tpath('etc/apt')],
                ]
            if self.model.network.has_network:
                cmds.append([
                    sys.executable, "-m", "curtin", "in-target", "-t",
                    "/target", "--", "apt-get", "update",
                    ])
            else:
                cmds.append(["umount", self.tpath('var/lib/apt/lists')])
        for cmd in cmds:
            await arun_command(self.logged_command(cmd), check=True)

    @with_context(description="downloading and installing security updates")
    async def run_unattended_upgrades(self, context):
        target_tmp = os.path.join(self.model.target, "tmp")
        os.makedirs(target_tmp, exist_ok=True)
        apt_conf = tempfile.NamedTemporaryFile(
            dir=target_tmp, delete=False, mode='w')
        apt_conf.write(uu_apt_conf)
        apt_conf.close()
        env = os.environ.copy()
        env["APT_CONFIG"] = apt_conf.name[len(self.model.target):]
        self.unattended_upgrades_ctx = context
        if self.opts.dry_run:
            self.unattended_upgrades_proc = await astart_command(
                self.logged_command(
                    ["sleep", str(5/self.app.scale_factor)]), env=env)
        else:
            self.unattended_upgrades_proc = await astart_command(
                self.logged_command([
                    sys.executable, "-m", "curtin", "in-target", "-t",
                    "/target", "--", "unattended-upgrades", "-v",
                    ]), env=env)
        await self.unattended_upgrades_proc.communicate()
        self.unattended_upgrades_proc = None
        self.unattended_upgrades_ctx = None
        os.remove(apt_conf.name)

    async def stop_unattended_upgrades(self):
        self.progress_view.event_finish(self.unattended_upgrades_ctx)
        with self.unattended_upgrades_ctx.parent.child(
                "stop_unattended_upgrades",
                "cancelling update"):
            if self.opts.dry_run:
                await asyncio.sleep(1)
                self.unattended_upgrades_proc.terminate()
            else:
                await arun_command(self.logged_command([
                    'chroot', '/target',
                    '/usr/share/unattended-upgrades/'
                    'unattended-upgrade-shutdown',
                    '--stop-only',
                    ]), check=True)

    async def _click_reboot(self):
        if self.unattended_upgrades_ctx is not None:
            self.update_state(InstallState.UU_CANCELLING)
            await self.stop_unattended_upgrades()
        self.reboot_clicked.set()

    def click_reboot(self):
        schedule_task(self._click_reboot())

    def make_ui(self):
        schedule_task(self.move_on())
        return self.progress_view

    def run_answers(self):
        pass


uu_apt_conf = """\
# Config for the unattended-upgrades run to avoid failing on battery power or
# a metered connection.
Unattended-Upgrade::OnlyOnACPower "false";
Unattended-Upgrade::Skip-Updates-On-Metered-Connections "true";
"""
