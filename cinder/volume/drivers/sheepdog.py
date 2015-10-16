#    Copyright 2012 OpenStack Foundation
#    Copyright (c) 2013 Zelin.io
#    Copyright (C) 2015 Nippon Telegraph and Telephone Corporation.
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
SheepDog Volume Driver.

"""
import errno
import eventlet
import io
import re

from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import units
from six.moves import urllib

from cinder import exception
from cinder.i18n import _, _LE, _LW
from cinder.image import image_utils
from cinder import utils
from cinder.volume import driver

# snapshot name of glance image
GLANCE_SNAPNAME = 'glance-image'

LOG = logging.getLogger(__name__)

sheepdog_opts = [
    cfg.StrOpt('sheepdog_store_address',
               default='127.0.0.1',
               help=('IP address of sheep daemon.')),
    cfg.IntOpt('sheepdog_store_port',
               min=1, max=65535,
               default=7000,
               help=('Port of sheep daemon.'))
]

CONF = cfg.CONF
CONF.import_opt("image_conversion_dir", "cinder.image.image_utils")
CONF.register_opts(sheepdog_opts)


class SheepdogClient(object):
    """Sheepdog command executor."""
    QEMU_SHEEPDOG_PREFIX = 'sheepdog:'
    DOG_RESP_CONNECTION_ERROR = 'failed to connect to'
    DOG_RESP_CLUSTER_RUNNING = 'Cluster status: running'
    DOG_RESP_CLUSTER_NOT_FORMATTED = ('Cluster status: '
                                      'Waiting for cluster to be formatted')
    DOG_RESP_CLUSTER_WAITING = ('Cluster status: '
                                'Waiting for other nodes to join cluster')
    DOG_RESP_GET_NO_NODE_INFO = 'Cannot get information from any nodes'
    DOG_RESP_VDI_ALREADY_EXISTS = ': VDI exists already'
    DOG_RESP_VDI_NOT_FOUND = ': No VDI found'
    DOG_RESP_VDI_SHRINK_NOT_SUPPORT = 'Shrinking VDIs is not implemented'
    DOG_RESP_VDI_SIZE_TOO_LARGE = 'New VDI size is too large'
    DOG_RESP_SNAPSHOT_VDI_NOT_FOUND = ': No VDI found'
    DOG_RESP_SNAPSHOT_NOT_FOUND = ': Failed to find requested tag'
    DOG_RESP_SNAPSHOT_EXISTED = 'tag (%(snapname)s) is existed'
    QEMU_IMG_RESP_CONNECTION_ERROR = ('Failed to connect socket: '
                                      'Connection refused')
    QEMU_IMG_RESP_ALREADY_EXISTS = ': VDI exists already'
    QEMU_IMG_RESP_SNAPSHOT_NOT_FOUND = 'Failed to find the requested tag'
    QEMU_IMG_RESP_VDI_NOT_FOUND = 'No vdi found'
    QEMU_IMG_RESP_SIZE_TOO_LARGE = 'An image is too large.'
    QEMU_IMG_RESP_FILE_NOT_FOUND = 'No such file or directory'
    QEMU_IMG_RESP_PERMISSION_DENIED = 'Permission denied'
    QEMU_IMG_RESP_INVALID_DRIVER = 'Unknown driver'
    QEMU_IMG_RESP_INVALID_FORMAT = 'Unknown file format'

    STATS_PATTERN = re.compile(r'[\w\s%]*Total\s(\d+)\s(\d+)\s(\d+)*')

    def __init__(self, addr, port):
        self.addr = addr
        self.port = port

    def _run_dog(self, command, subcommand, *params):
        cmd = ('env', 'LC_ALL=C', 'LANG=C', 'dog', command, subcommand,
               '-a', self.addr, '-p', self.port) + params
        try:
            return utils.execute(*cmd)
        except OSError as e:
            with excutils.save_and_reraise_exception():
                if e.errno == errno.ENOENT:
                    msg = _LE('Sheepdog is not installed. '
                              'OSError: command is %s.')
                else:
                    msg = _LE('OSError: command is %s.')
                LOG.error(msg, cmd)
        except processutils.ProcessExecutionError as e:
            raise exception.SheepdogCmdError(
                cmd=e.cmd,
                exit_code=e.exit_code,
                stdout=e.stdout.replace('\n', '\\n'),
                stderr=e.stderr.replace('\n', '\\n'))

    def _run_qemu_img(self, command, *params):
        """Executes qemu-img command wrapper"""
        cmd = ['env', 'LC_ALL=C', 'LANG=C', 'qemu-img', command]
        for param in params:
            if param.startswith(self.QEMU_SHEEPDOG_PREFIX):
                # replace 'sheepdog:vdiname[:snapshotname]' to
                #         'sheepdog:addr:port:vdiname[:snapshotname]'
                param = param.replace(self.QEMU_SHEEPDOG_PREFIX,
                                      '%(prefix)s%(addr)s:%(port)s:' %
                                      {'prefix': self.QEMU_SHEEPDOG_PREFIX,
                                       'addr': self.addr, 'port': self.port},
                                      1)
            cmd.append(param)
        try:
            return utils.execute(*cmd)
        except OSError as e:
            with excutils.save_and_reraise_exception():
                if e.errno == errno.ENOENT:
                    msg = _LE('Qemu-img is not installed. '
                              'OSError: command is %(cmd)s.')
                else:
                    msg = _LE('OSError: command is %(cmd)s.')
                LOG.error(msg, {'cmd': tuple(cmd)})
        except processutils.ProcessExecutionError as e:
            raise exception.SheepdogCmdError(
                cmd=e.cmd,
                exit_code=e.exit_code,
                stdout=e.stdout.replace('\n', '\\n'),
                stderr=e.stderr.replace('\n', '\\n'))

    def check_cluster_status(self):
        try:
            (_stdout, _stderr) = self._run_dog('cluster', 'info')
        except exception.SheepdogCmdError as e:
            cmd = e.kwargs['cmd']
            _stderr = e.kwargs['stderr']
            with excutils.save_and_reraise_exception():
                if _stderr.startswith(self.DOG_RESP_CONNECTION_ERROR):
                    msg = _LE('Failed to connect to sheep daemon. '
                              'addr: %(addr)s, port: %(port)s')
                    LOG.error(msg, {'addr': self.addr, 'port': self.port})
                else:
                    LOG.error(_LE('Failed to check cluster status.'
                                  '(command: %s)'), cmd)

        if _stdout.startswith(self.DOG_RESP_CLUSTER_RUNNING):
            LOG.debug('Sheepdog cluster is running.')
            return

        reason = _('Invalid sheepdog cluster status.')
        if _stdout.startswith(self.DOG_RESP_CLUSTER_NOT_FORMATTED):
            reason = _('Cluster is not formatted. '
                       'You should probably perform "dog cluster format".')
        elif _stdout.startswith(self.DOG_RESP_CLUSTER_WAITING):
            reason = _('Waiting for all nodes to join cluster. '
                       'Ensure all sheep daemons are running.')
        raise exception.SheepdogError(reason=reason)

    def create(self, vdiname, size):
        try:
            self._run_dog('vdi', 'create', vdiname, '%sG' % size)
        except exception.SheepdogCmdError as e:
            _stderr = e.kwargs['stderr']
            with excutils.save_and_reraise_exception():
                if _stderr.startswith(self.DOG_RESP_CONNECTION_ERROR):
                    LOG.error(_LE("Failed to connect to sheep daemon. "
                              "addr: %(addr)s, port: %(port)s"),
                              {'addr': self.addr, 'port': self.port})
                elif _stderr.rstrip('\\n').endswith(
                        self.DOG_RESP_VDI_ALREADY_EXISTS):
                    LOG.error(_LE('Volume already exists. %s'), vdiname)
                else:
                    LOG.error(_LE('Failed to create volume. %s'), vdiname)

    def delete(self, vdiname):
        try:
            (_stdout, _stderr) = self._run_dog('vdi', 'delete', vdiname)
            if _stderr.rstrip().endswith(self.DOG_RESP_VDI_NOT_FOUND):
                LOG.warning(_LW('Volume not found. %s'), vdiname)
            elif _stderr.startswith(self.DOG_RESP_CONNECTION_ERROR):
                # NOTE(tishizaki)
                # Dog command does not return error_code although
                # dog command cannot connect to sheep process.
                # That is a Sheepdog's bug.
                # To avoid a Sheepdog's bug, now we need to check stderr.
                # If Sheepdog has been fixed, this check logic is needed
                # by old Sheepdog users.
                reason = (_('Failed to connect to sheep daemon. '
                          'addr: %(addr)s, port: %(port)s'),
                          {'addr': self.addr, 'port': self.port})
                raise exception.SheepdogError(reason=reason)
        except exception.SheepdogCmdError as e:
            _stderr = e.kwargs['stderr']
            with excutils.save_and_reraise_exception():
                if _stderr.startswith(self.DOG_RESP_CONNECTION_ERROR):
                    LOG.error(_LE('Failed to connect to sheep daemon. '
                              'addr: %(addr)s, port: %(port)s'),
                              {'addr': self.addr, 'port': self.port})
                else:
                    LOG.error(_LE('Failed to delete volume. %s'), vdiname)

    def create_snapshot(self, vdiname, snapname):
        try:
            self._run_dog('vdi', 'snapshot', '-s', snapname, vdiname)
        except exception.SheepdogCmdError as e:
            cmd = e.kwargs['cmd']
            _stderr = e.kwargs['stderr']
            with excutils.save_and_reraise_exception():
                if _stderr.startswith(self.DOG_RESP_CONNECTION_ERROR):
                    LOG.error(_LE('Failed to connect to sheep daemon. '
                              'addr: %(addr)s, port: %(port)s'),
                              {'addr': self.addr, 'port': self.port})
                elif _stderr.rstrip('\\n').endswith(
                        self.DOG_RESP_SNAPSHOT_VDI_NOT_FOUND):
                    LOG.error(_LE('Volume "%s" not found. Please check the '
                                  'results of "dog vdi list".'), vdiname)
                elif _stderr.rstrip('\\n').endswith(
                        self.DOG_RESP_SNAPSHOT_EXISTED %
                        {'snapname': snapname}):
                    LOG.error(_LE('Snapshot "%s" already exists.'), snapname)
                else:
                    LOG.error(_LE('Failed to create snapshot. (command: %s)'),
                              cmd)

    def delete_snapshot(self, vdiname, snapname):
        try:
            (_stdout, _stderr) = self._run_dog('vdi', 'delete', '-s',
                                               snapname, vdiname)
            if _stderr.rstrip().endswith(self.DOG_RESP_SNAPSHOT_NOT_FOUND):
                LOG.warning(_LW('Snapshot "%s" not found.'), snapname)
            elif _stderr.rstrip().endswith(self.DOG_RESP_VDI_NOT_FOUND):
                LOG.warning(_LW('Volume "%s" not found.'), vdiname)
            elif _stderr.startswith(self.DOG_RESP_CONNECTION_ERROR):
                # NOTE(tishizaki)
                # Dog command does not return error_code although
                # dog command cannot connect to sheep process.
                # That is a Sheepdog's bug.
                # To avoid a Sheepdog's bug, now we need to check stderr.
                # If Sheepdog has been fixed, this check logic is needed
                # by old Sheepdog users.
                reason = (_('Failed to connect to sheep daemon. '
                          'addr: %(addr)s, port: %(port)s'),
                          {'addr': self.addr, 'port': self.port})
                raise exception.SheepdogError(reason=reason)
        except exception.SheepdogCmdError as e:
            cmd = e.kwargs['cmd']
            _stderr = e.kwargs['stderr']
            with excutils.save_and_reraise_exception():
                if _stderr.startswith(self.DOG_RESP_CONNECTION_ERROR):
                    msg = _LE('Failed to connect to sheep daemon. '
                              'addr: %(addr)s, port: %(port)s')
                    LOG.error(msg, {'addr': self.addr, 'port': self.port})
                else:
                    LOG.error(_LE('Failed to delete snapshot. (command: %s)'),
                              cmd)

    def clone(self, src_vdiname, src_snapname, dst_vdiname, size):
        try:
            self._run_qemu_img('create', '-b',
                               'sheepdog:%(src_vdiname)s:%(src_snapname)s' %
                               {'src_vdiname': src_vdiname,
                                'src_snapname': src_snapname},
                               'sheepdog:%s' % dst_vdiname, '%sG' % size)
        except exception.SheepdogCmdError as e:
            cmd = e.kwargs['cmd']
            _stderr = e.kwargs['stderr']
            with excutils.save_and_reraise_exception():
                if self.QEMU_IMG_RESP_CONNECTION_ERROR in _stderr:
                    LOG.error(_LE('Failed to connect to sheep daemon. '
                                  'addr: %(addr)s, port: %(port)s'),
                              {'addr': self.addr, 'port': self.port})
                elif self.QEMU_IMG_RESP_ALREADY_EXISTS in _stderr:
                    LOG.error(_LE('Clone volume "%s" already exists. '
                              'Please check the results of "dog vdi list".'),
                              dst_vdiname)
                elif self.QEMU_IMG_RESP_VDI_NOT_FOUND in _stderr:
                    LOG.error(_LE('Src Volume "%s" not found. '
                              'Please check the results of "dog vdi list".'),
                              src_vdiname)
                elif self.QEMU_IMG_RESP_SNAPSHOT_NOT_FOUND in _stderr:
                    LOG.error(_LE('Snapshot "%s" not found. '
                              'Please check the results of "dog vdi list".'),
                              src_snapname)
                elif self.QEMU_IMG_RESP_SIZE_TOO_LARGE in _stderr:
                    LOG.error(_LE('Volume size "%sG" is too large.'), size)
                else:
                    LOG.error(_LE('Failed to clone volume.(command: %s)'), cmd)

    def resize(self, vdiname, size):
        size = int(size) * units.Gi
        try:
            (_stdout, _stderr) = self._run_dog('vdi', 'resize', vdiname, size)
        except exception.SheepdogCmdError as e:
            _stderr = e.kwargs['stderr']
            with excutils.save_and_reraise_exception():
                if _stderr.startswith(self.DOG_RESP_CONNECTION_ERROR):
                    LOG.error(_LE('Failed to connect to sheep daemon. '
                                  'addr: %(addr)s, port: %(port)s'),
                              {'addr': self.addr, 'port': self.port})
                elif _stderr.rstrip('\\n').endswith(
                        self.DOG_RESP_VDI_NOT_FOUND):
                    LOG.error(_LE('Failed to resize vdi. vdi not found. %s'),
                              vdiname)
                elif _stderr.startswith(self.DOG_RESP_VDI_SHRINK_NOT_SUPPORT):
                    LOG.error(_LE('Failed to resize vdi. '
                                  'Shrinking vdi not supported. '
                                  'vdi: %(vdiname)s new size: %(size)s'),
                              {'vdiname': vdiname, 'size': size})
                elif _stderr.startswith(self.DOG_RESP_VDI_SIZE_TOO_LARGE):
                    LOG.error(_LE('Failed to resize vdi. '
                                  'Too large volume size. '
                                  'vdi: %(vdiname)s new size: %(size)s'),
                              {'vdiname': vdiname, 'size': size})
                else:
                    LOG.error(_LE('Failed to resize vdi. '
                                  'vdi: %(vdiname)s new size: %(size)s'),
                              {'vdiname': vdiname, 'size': size})

    def convert(self, src_path, dst_path, src_fmt='raw', dst_fmt='raw'):
        params = ('-f', src_fmt, '-t', 'none', '-O', dst_fmt,
                  src_path, dst_path)
        try:
            (_stdout, _stderr) = self._run_qemu_img('convert', *params)
        except exception.SheepdogCmdError as e:
            _stderr = e.kwargs['stderr']
            with excutils.save_and_reraise_exception():
                if self.QEMU_IMG_RESP_CONNECTION_ERROR in _stderr:
                    LOG.error(_LE('Failed to connect to sheep daemon.'
                                  ' addr: %(addr)s, port: %(port)s'),
                              {'addr': self.addr, 'port': self.port})
                elif self.QEMU_IMG_RESP_VDI_NOT_FOUND in _stderr:
                    LOG.error(_LE('Convert failed. VDI not found.'
                                  ' Please check %(src_path)s exist.'),
                              {'src_path': src_path})
                elif self.QEMU_IMG_RESP_ALREADY_EXISTS in _stderr:
                    LOG.error(_LE('VDI already exists.'
                                  ' Please check %(dst_path)s '
                                  'is not duplicated.'),
                              {'dst_path': dst_path})
                elif self.QEMU_IMG_RESP_FILE_NOT_FOUND in _stderr:
                    LOG.error(_LE('Convert failed. File not found.'
                                  ' Please check %(src_path)s exist. '),
                              {'src_path': src_path})
                elif self.QEMU_IMG_RESP_PERMISSION_DENIED in _stderr:
                    LOG.error(_LE('Convert failed. Permission denied.'
                                  ' Please check permission of'
                                  ' source path: %(src_path)s and'
                                  ' destination path: %(dst_path)s'),
                              {'src_path': src_path, 'dst_path': dst_path})
                elif self.QEMU_IMG_RESP_INVALID_FORMAT in _stderr:
                    LOG.error(_LE('Convert failed. Not supported format.'
                                  ' Please check format %(dst_format)s'
                                  ' is valid'),
                              {'dst_format': dst_fmt})
                elif self.QEMU_IMG_RESP_INVALID_DRIVER in _stderr:
                    LOG.error(_LE('Convert failed. Not supported driver used.'
                                  ' Please check driver name %(src_format)s'
                                  ' is valid'),
                              {'src_format': src_fmt})
                else:
                    LOG.error(_LE('Convert failed.'
                                  ' source path: %(src_path)s '
                                  ' destination path: %(dst_path)s'
                                  ' source format %(src_format)s'
                                  ' destination format %(dst_format)s'),
                              {'src_path': src_path, 'dst_path': dst_path,
                               'src_fmt': src_fmt, 'sdt_fmt': dst_fmt})

    def _is_cloneable(self, image_location, image_meta):
        """Check the image can be clone or not."""
        _cloneable = False
        _snapshot = False
        if image_meta['disk_format'] != 'raw':
            LOG.debug('Image clone requires image format to be '
                      '"raw" but image %(image_location)s is %(image_meta)s.',
                      {'image_location': image_location,
                       'image_meta': image_meta['disk_format']})
            return _cloneable, _snapshot

        # The image location would be like
        # "sheepdog://Alice"
        try:
            volume_name = self._parse_location(image_location)
        except exception.ImageUnacceptable as e:
            LOG.debug('%(image_location)s does not match the sheepdog format. '
                      'reason: %(err)s',
                      {'image_location': image_location, 'err': e})
            return _cloneable, _snapshot

        # check whether volume is stored in sheepdog
        (_stdout, _stderr) = self._run_dog('vdi', 'list', '-r', volume_name)
        if _stdout == '':
            LOG.debug('Image %s is not stored in sheepdog.', volume_name)
            return _cloneable, _snapshot

        if GLANCE_SNAPNAME not in _stdout:
            LOG.debug('Image %s is not a snapshot volume.', volume_name)
            _cloneable = True
            return _cloneable, _snapshot

        _cloneable = True
        _snapshot = True
        return _cloneable, _snapshot

    def _parse_location(self, location):
        """Check Glance and Cinder use the same sheepdog pool or not."""
        if location is None:
            reason = _('image_location is NULL.')
            raise exception.ImageUnacceptable(image_id=location, reason=reason)

        prefix = 'sheepdog://'
        if not location.startswith(prefix):
            reason = _('Not stored in sheepdog.')
            raise exception.ImageUnacceptable(image_id=location, reason=reason)
        pieces = list(map(urllib.parse.unquote,
                      location[len(prefix):].split('/')))
        if len(pieces) != 1:
            reason = _('Not a sheepdog image.')
            raise exception.ImageUnacceptable(image_id=location, reason=reason)
        if len(pieces[0]) == 0:
            reason = _('Blank components.')
            raise exception.ImageUnacceptable(image_id=location, reason=reason)

        return pieces[0]

    def get_disk_capacity(self):
        try:
            (_stdout, _stderr) = self._run_dog('node', 'info', '-r')
        except exception.SheepdogCmdError as e:
            _stderr = e.kwargs['stderr']
            with excutils.save_and_reraise_exception():
                if _stderr.startswith(self.DOG_RESP_CONNECTION_ERROR):
                    LOG.exception(_LE('Failed to connect to sheep daemon. '
                                  'addr: %(addr)s, port: %(port)s'),
                                  {'addr': self.addr, 'port': self.port})
                elif _stderr.startswith(self.DOG_RESP_GET_NO_NODE_INFO):
                    LOG.exception(_LE('Failed to get any nodes info of the '
                                  'cluster. Please check "dog cluster info."'))
                else:
                    LOG.exception(_LE('Failed to get disk usage from the '
                                  'Storage. addr: %(addr)s, port: %(port)s'),
                                  {'addr': self.addr, 'port': self.port})

        m = self.STATS_PATTERN.match(_stdout)
        try:
            total = float(m.group(1))
            used = float(m.group(2))
            free = float(m.group(3))
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception(_LE('Failed to parse stdout of '
                                  '"dog node list -r". stdout: %s'), _stdout)

        total_gb = total / units.Gi
        used_gb = used / units.Gi
        free_gb = free / units.Gi

        return (total_gb, used_gb, free_gb)


class SheepdogIOWrapper(io.RawIOBase):
    """File-like object with Sheepdog backend."""

    def __init__(self, volume, snapshot_name=None):
        self._vdiname = volume['name']
        self._snapshot_name = snapshot_name
        self._offset = 0
        # SheepdogIOWrapper instance becomes invalid if a write error occurs.
        self._valid = True

    def _execute(self, cmd, data=None):
        try:
            # NOTE(yamada-h): processutils.execute causes busy waiting
            # under eventlet.
            # To avoid wasting CPU resources, it should not be used for
            # the command which takes long time to execute.
            # For workaround, we replace a subprocess module with
            # the original one while only executing a read/write command.
            _processutils_subprocess = processutils.subprocess
            processutils.subprocess = eventlet.patcher.original('subprocess')
            return processutils.execute(*cmd, process_input=data)[0]
        except (processutils.ProcessExecutionError, OSError):
            self._valid = False
            msg = _('Sheepdog I/O Error, command was: "%s".') % ' '.join(cmd)
            raise exception.VolumeDriverException(message=msg)
        finally:
            processutils.subprocess = _processutils_subprocess

    def read(self, length=None):
        if not self._valid:
            msg = _('An error occurred while reading volume "%s".'
                    ) % self._vdiname
            raise exception.VolumeDriverException(message=msg)

        cmd = ['dog', 'vdi', 'read']
        if self._snapshot_name:
            cmd.extend(('-s', self._snapshot_name))
        cmd.extend((self._vdiname, self._offset))
        if length:
            cmd.append(length)
        data = self._execute(cmd)
        self._offset += len(data)
        return data

    def write(self, data):
        if not self._valid:
            msg = _('An error occurred while writing to volume "%s".'
                    ) % self._vdiname
            raise exception.VolumeDriverException(message=msg)

        length = len(data)
        cmd = ('dog', 'vdi', 'write', self._vdiname, self._offset, length)
        self._execute(cmd, data)
        self._offset += length
        return length

    def seek(self, offset, whence=0):
        if not self._valid:
            msg = _('An error occured while seeking for volume "%s".'
                    ) % self._vdiname
            raise exception.VolumeDriverException(message=msg)

        if whence == 0:
            # SEEK_SET or 0 - start of the stream (the default);
            # offset should be zero or positive
            new_offset = offset
        elif whence == 1:
            # SEEK_CUR or 1 - current stream position; offset may be negative
            new_offset = self._offset + offset
        else:
            # SEEK_END or 2 - end of the stream; offset is usually negative
            # TODO(yamada-h): Support SEEK_END
            raise IOError(_("Invalid argument - whence=%s not supported.") %
                          whence)

        if new_offset < 0:
            raise IOError(_("Invalid argument - negative seek offset."))

        self._offset = new_offset

    def tell(self):
        return self._offset

    def flush(self):
        pass

    def fileno(self):
        """Sheepdog does not have support for fileno so we raise IOError.

        Raising IOError is recommended way to notify caller that interface is
        not supported - see http://docs.python.org/2/library/io.html#io.IOBase
        """
        raise IOError(_("fileno is not supported by SheepdogIOWrapper"))


class SheepdogDriver(driver.VolumeDriver):
    """Executes commands relating to Sheepdog Volumes."""

    VERSION = "1.0.0"

    def __init__(self, *args, **kwargs):
        super(SheepdogDriver, self).__init__(*args, **kwargs)
        self.client = SheepdogClient(CONF.sheepdog_store_address,
                                     CONF.sheepdog_store_port)
        self._stats = {}

    def check_for_setup_error(self):
        self.client.check_cluster_status()

    def clone_image(self, context, volume,
                    image_location, image_meta,
                    image_service):
        """Create a volume efficiently from an existing image."""
        image_location = image_location[0] if image_location else None
        (_cloneable, _snapshot) = self.client._is_cloneable(image_location,
                                                            image_meta)
        if not _cloneable:
            return {}, False

        # The image location would be like
        # "sheepdog://Alice"
        source_name = self.client._parse_location(image_location)
        source_vol = {
            'name': source_name,
            'size': image_meta['size']
        }

        try:
            if _snapshot:
                self.client.clone(source_name, GLANCE_SNAPNAME,
                                  volume.name, volume.size)
            else:
                self.create_cloned_volume(volume,
                                          source_vol)
        except exception.SheepdogCmdError:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE('Failed to create clone image : %s.'),
                          volume.name)

        vol_path = self.local_path(volume)
        return {'provider_location': vol_path}, True

    def create_cloned_volume(self, volume, src_vref):
        """Clone a sheepdog volume from another volume."""

        snapshot_name = 'temp-snapshot-' + src_vref['name']
        snapshot = {
            'name': snapshot_name,
            'volume_name': src_vref['name'],
            'volume_size': src_vref['size'],
        }

        self.client.create_snapshot(snapshot['volume_name'], snapshot_name)

        try:
            self.client.clone(snapshot['volume_name'], snapshot_name,
                              volume.name, volume.size)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE('Failed to create cloned volume %s.'),
                          volume.name)
        finally:
            # Delete temp Snapshot
            self.client.delete_snapshot(snapshot['volume_name'], snapshot_name)

    def create_volume(self, volume):
        """Create a sheepdog volume."""
        self.client.create(volume.name, volume.size)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Create a sheepdog volume from a snapshot."""
        self.client.clone(snapshot.volume_name, snapshot.name,
                          volume.name, volume.size)

    def delete_volume(self, volume):
        """Delete a logical volume."""
        self.client.delete(volume.name)

    def copy_image_to_volume(self, context, volume, image_service, image_id):
        with image_utils.temporary_file() as tmp:
            # (wenhao): we don't need to convert to raw for sheepdog.
            image_utils.fetch_verify_image(context, image_service,
                                           image_id, tmp)

            # remove the image created by import before this function.
            # see volume/drivers/manager.py:_create_volume
            self.client.delete(volume.name)
            # convert and store into sheepdog
            self.client.convert(tmp, 'sheepdog:%s' % volume.name)
            try:
                self.client.resize(volume.name, volume.size)
            except Exception:
                with excutils.save_and_reraise_exception():
                    self.client.delete(volume.name)

    def copy_volume_to_image(self, context, volume, image_service, image_meta):
        """Copy the volume to the specified image."""
        image_id = image_meta['id']
        try:
            with image_utils.temporary_file() as tmp:
                self.client.convert('sheepdog:%s' % volume.name, tmp)
                with open(tmp, 'rb') as image_file:
                    image_service.update(context, image_id, {}, image_file)
        except Exception:
            with excutils.save_and_reraise_exception():
                msg = _LE('Failed to copy volume: %(vdiname)s to '
                          'image: %(path)s.')
                LOG.error(msg, {'vdiname': volume.name, 'path': tmp})

    def create_snapshot(self, snapshot):
        """Create a sheepdog snapshot."""
        self.client.create_snapshot(snapshot.volume_name, snapshot.name)

    def delete_snapshot(self, snapshot):
        """Delete a sheepdog snapshot."""
        self.client.delete_snapshot(snapshot.volume_name, snapshot.name)

    def local_path(self, volume):
        """Get volume path."""
        return "sheepdog:%s" % volume.name

    def ensure_export(self, context, volume):
        """Safely and synchronously recreate an export for a logical volume."""
        pass

    def create_export(self, context, volume, connector):
        """Export a volume."""
        pass

    def remove_export(self, context, volume):
        """Remove an export for a logical volume."""
        pass

    def initialize_connection(self, volume, connector):
        return {
            'driver_volume_type': 'sheepdog',
            'data': {
                'name': volume['name'],
                'hosts': [CONF.sheepdog_store_address],
                'ports': [str(CONF.sheepdog_store_port)],
            }
        }

    def terminate_connection(self, volume, connector, **kwargs):
        pass

    def _update_volume_stats(self):
        stats = {}

        backend_name = "sheepdog"
        if self.configuration:
            backend_name = self.configuration.safe_get('volume_backend_name')
        stats["volume_backend_name"] = backend_name or 'sheepdog'
        stats['vendor_name'] = 'Open Source'
        stats['driver_version'] = self.VERSION
        stats['storage_protocol'] = 'sheepdog'
        stats['total_capacity_gb'] = 'unknown'
        stats['free_capacity_gb'] = 'unknown'
        stats['reserved_percentage'] = 0
        stats['QoS_support'] = False

        try:
            (total_gb, used_gb, free_gb) = self.client.get_disk_capacity()
            stats['total_capacity_gb'] = total_gb
            stats['free_capacity_gb'] = free_gb
        except Exception:
            LOG.exception(_LE('error refreshing volume stats'))

        self._stats = stats

    def get_volume_stats(self, refresh=False):
        if refresh:
            self._update_volume_stats()
        return self._stats

    def extend_volume(self, volume, new_size):
        """Extend an Existing Volume."""
        self.client.resize(volume.name, new_size)
        LOG.debug('Extend volume from %(old_size)s GB to %(new_size)s GB.',
                  {'old_size': volume.size, 'new_size': new_size})

    def backup_volume(self, context, backup, backup_service):
        """Create a new backup from an existing volume."""
        src_volume = self.db.volume_get(context, backup.volume_id)
        temp_snapshot_name = 'tmp-snap-%s' % src_volume.name

        # NOTE(tishizaki): If previous backup_volume operation has failed,
        # a temporary snapshot for previous operation may exist.
        # So, the old snapshot must be deleted before backup_volume.
        # Sheepdog 0.9 or later 'delete_snapshot' operation
        # is done successfully, although target snapshot does not exist.
        # However, sheepdog 0.8 or before 'delete_snapshot' operation
        # is failed, and raise ProcessExecutionError when target snapshot
        # does not exist.
        try:
            self.client.delete_snapshot(src_volume.name, temp_snapshot_name)
        except (exception.SheepdogCmdError):
            pass

        try:
            self.client.create_snapshot(src_volume.name, temp_snapshot_name)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE('Failed to create a temporary snapshot for '
                              'volume "%s".'), src_volume.name)

        try:
            sheepdog_fd = SheepdogIOWrapper(src_volume, temp_snapshot_name)
            backup_service.backup(backup, sheepdog_fd)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE('Failed to backup volume "%s".'),
                          src_volume.name)
        finally:
            self.client.delete_snapshot(src_volume.name, temp_snapshot_name)

    def restore_backup(self, context, backup, volume, backup_service):
        """Restore an existing backup to a new or existing volume."""
        sheepdog_fd = SheepdogIOWrapper(volume)
        backup_service.restore(backup, volume.id, sheepdog_fd)
