# -*- coding: utf-8 -*-
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2016 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import copy
import os
import sys
from typing import List, TYPE_CHECKING, Tuple, NamedTuple, Any, Dict, Optional

from . import bitcoin
from . import keystore
from . import mnemonic
from .bip32 import is_bip32_derivation, xpub_type, normalize_bip32_derivation, BIP32Node
from .gui.qt.advanced_option_mixin import AdvancedOptionMixin
from .i18n import _
from .keystore import bip44_derivation, purpose48_derivation
from .logging import Logger
from .plugin import Plugins, HardwarePluginLibraryUnavailable
from .plugins.hw_wallet.plugin import OutdatedHwFirmwareException, HW_PluginBase
from .simple_config import SimpleConfig
from .storage import (WalletStorage, StorageEncryptionVersion,
                      get_derivation_used_for_hw_device_encryption)
from .three_keys import short_mnemonic
from .util import UserCancelled, InvalidPassword
from .wallet import (wallet_types)

if TYPE_CHECKING:
    from .plugin import DeviceInfo

# hardware device setup purpose
HWD_SETUP_NEW_WALLET, HWD_SETUP_DECRYPT_WALLET = range(0, 2)


class ScriptTypeNotSupported(Exception): pass


class GoBack(Exception): pass


class WizardStackItem(NamedTuple):
    action: Any
    args: Any
    kwargs: Dict[str, Any]
    storage_data: dict


class WizardWalletPasswordSetting(NamedTuple):
    password: Optional[str]
    encrypt_storage: bool
    storage_enc_version: StorageEncryptionVersion
    encrypt_keystore: bool


class BaseWizard(Logger, AdvancedOptionMixin):

    def __init__(self, config: SimpleConfig, plugins: Plugins):
        super(BaseWizard, self).__init__()
        Logger.__init__(self)
        self.config = config
        self.plugins = plugins
        self.data = {}
        self.pw_args = None  # type: Optional[WizardWalletPasswordSetting]
        self._stack = []  # type: List[WizardStackItem]
        self.plugin = None
        self.keystores = []
        self.is_kivy = config.get('gui') == 'kivy'
        self.seed_type = None

    def set_icon(self, icon):
        pass

    def run(self, *args, **kwargs):
        action = args[0]
        args = args[1:]
        storage_data = copy.deepcopy(self.data)
        self._stack.append(WizardStackItem(action, args, kwargs, storage_data))
        if not action:
            return
        if type(action) is tuple:
            self.plugin, action = action
        if self.plugin and hasattr(self.plugin, action):
            f = getattr(self.plugin, action)
            f(self, *args, **kwargs)
        elif hasattr(self, action):
            f = getattr(self, action)
            f(*args, **kwargs)
        else:
            raise Exception("unknown action", action)

    def can_go_back(self):
        return len(self._stack) > 1

    def go_back(self):
        if not self.can_go_back():
            return
        # pop 'current' frame
        self._stack.pop()
        # pop 'previous' frame
        stack_item = self._stack.pop()
        # try to undo side effects since we last entered 'previous' frame
        # FIXME only self.storage is properly restored
        self.data = copy.deepcopy(stack_item.storage_data)
        # rerun 'previous' frame
        self.run(stack_item.action, *stack_item.args, **stack_item.kwargs)

    def reset_stack(self):
        self._stack = []

    def new(self):
        title = _("Create new wallet")
        message = '\n'.join([
            _("Choose the type of wallet")
        ])
        base_wallet_kinds = [
            ('2-key', _('2-Key Vault')),
            ('3-key', _('3-Key Vault')),
            ('standard', _('Standard')),
        ]
        advanced_wallet_kinds = [
            ('multisig', _('Multi-Signature Standard')),
            ('imported', _('Import external watch-only BTCV addresses or private keys')),
        ]

        link = 'https://translations.bitcoinvault.global/pdf/BTCV_Tutorial/BTCV-ShortTutorial-2Pager-en.pdf'
        h_txt = _('Read more')
        hint = ' '.join([
            '<b> 2-Key Vault </b><br/>',
            _('Allows users to make Secure and Cancel transactions. Secure transactions are by default delayed by 144 '
              'blocks, which means approximately 24 hours. This gives users enough time to perform a Cancel '
              'transaction '
              'and reverse the transfer before it is confirmed on the blockchain in case of a hack or technical issue. '
              'Both transactions are secured with unique keys.'),
            '<br/><b><br/>',
            '3-Key Vault',
            '</b><br/>',
            _('Allows users to make Secure and Cancel transactions, just like in the 2-Key Vault wallet. But it also '
              'adds the possibility to perform quick Secure Fast transactions. These transfers are confirmed in '
              'approximately 10 minutes and can’t be reversed. All three transactions require unique '
              'keys to be performed.'),
            '<br/><br/><b>',
            'Standard',
            '</b><br/>',
            _('Allows users to make Standard transactions secured with one key.'),
            '<br/><br/>',
            f'<a href="{link}">{h_txt}</a>',
        ])

        base_choices = [pair for pair in base_wallet_kinds if pair[0] in wallet_types]
        advanced_choices = [pair for pair in advanced_wallet_kinds if pair[0] in wallet_types]
        self.choice_dialog_with_advanced_options(
            title=title, message=message, base_choices=base_choices, advanced_choices=advanced_choices,
            run_next=self.on_wallet_type, hint=hint
        )

    def upgrade_storage(self, storage):
        exc = None

        def on_finished():
            if exc is None:
                self.terminate(storage=storage)
            else:
                raise exc

        def do_upgrade():
            nonlocal exc
            try:
                storage.upgrade()
            except Exception as e:
                exc = e

        self.waiting_dialog(do_upgrade, _('Upgrading wallet format...'), on_finished=on_finished)

    def load_2fa(self):
        self.data['wallet_type'] = '2fa'
        self.data['use_trustedcoin'] = True
        self.plugin = self.plugins.load_plugin('trustedcoin')

    def on_wallet_type(self, choice):
        self.data['wallet_type'] = self.wallet_type = choice
        if choice == 'standard':
            action = 'choose_keystore'
        elif choice == 'multisig':
            action = 'choose_multisig'
        elif choice == '2fa':
            self.load_2fa()
            action = self.plugin.get_action(self.data)
        elif choice == 'imported':
            action = 'import_addresses_or_keys'
        elif choice in ['2-key', '3-key']:
            action = 'choose_multikey_wallet_type'
        self.run(action)

    def choose_multisig(self):
        def on_multisig(m, n):
            multisig_type = "%dof%d" % (m, n)
            self.data['wallet_type'] = multisig_type
            self.n = n
            self.run('choose_keystore')

        self.multisig_dialog(run_next=on_multisig)

    def choose_multikey_wallet_type(self):
        def process_choice(choice):
            if choice == 'multikey_standalone':
                self.data['multikey_type'] = 'standalone'
                if self.wallet_type == '2-key':
                    action = 'two_keys_standalone'
                elif self.wallet_type == '3-key':
                    action = 'three_keys_standalone'
                else:
                    raise Exception('Invalid multikey wallet type: ' + self.wallet_type)
            elif choice[:12] == 'multikey_2fa':
                self.data['multikey_type'] = '2fa'
                sub_action = choice[-7:]
                if self.wallet_type == '2-key':
                    action = 'two_keys_2fa' + sub_action
                elif self.wallet_type == '3-key':
                    action = 'three_keys_2fa' + sub_action
                else:
                    raise Exception('Invalid multikey wallet type: ' + self.wallet_type)
            else:
                raise Exception('Invalid choice: ' + choice)
            self.run(action)

        assert self.wallet_type in ['2-key', '3-key'], "Wrong multikey wallet type: " + self.wallet_type
        if self.wallet_type == '2-key':
            title = _('2-Key Vault')
        else:
            title = _('3-Key Vault')
        message = _('Do you want to use Gold Wallet as a transaction authenticator?')
        choices = [
            ('multikey_2fa_create', _('Use Gold Wallet and create a new wallet')),
            ('multikey_2fa_import', _('Use Gold Wallet and import an existing wallet')),
            ('multikey_standalone', _('Do not use Gold Wallet')),
        ]
        hint = ' '.join(['<b>Gold Wallet</b><br/><br/>',
                         _("A mobile wallet for BTCV. You can pair it with Electrum Vault and use "
                           "it to authorize transactions to additionally secure your funds.")
                         ])

        self.choice_dialog(title=title, message=message, choices=choices, run_next=process_choice, hint=hint)

    def two_keys_standalone(self):
        self.get_recovery_pubkey(run_next=self.on_two_keys_create)

    def two_keys_2fa_create(self):
        self.get_authenticator_pubkey(run_next=self.on_two_keys_create)

    def two_keys_2fa_import(self):
        self.get_authenticator_pubkey(run_next=self.on_two_keys_import)

    def on_two_keys_create(self, recovery_pubkey: str):
        self.data['recovery_pubkey'] = recovery_pubkey
        self.run('choose_keystore')

    def on_two_keys_import(self, recovery_pubkey: str):
        self.data['recovery_pubkey'] = recovery_pubkey
        self.run('restore_from_seed')

    def three_keys_standalone(self):
        def collect_instant_pubkey(instant_pubkey: str):
            self.data['instant_pubkey'] = instant_pubkey
            self.run('get_recovery_pubkey', run_next=self.on_three_keys_create, instant_key=self.data['instant_pubkey'])

        self.get_instant_pubkey(run_next=collect_instant_pubkey)

    def three_keys_2fa_create(self):
        def collect_instant_pubkey(instant_pubkey: str):
            self.data['instant_pubkey'] = instant_pubkey
            self.run('get_recovery_pubkey', run_next=self.on_three_keys_create, instant_key=self.data['instant_pubkey'])

        self.get_authenticator_pubkey(run_next=collect_instant_pubkey)

    def three_keys_2fa_import(self):
        def collect_instant_pubkey(instant_pubkey: str):
            self.data['instant_pubkey'] = instant_pubkey
            self.run('get_recovery_pubkey', run_next=self.on_three_keys_import, instant_key=self.data['instant_pubkey'])

        self.get_authenticator_pubkey(run_next=collect_instant_pubkey)

    def on_three_keys_create(self, recovery_pubkey: str):
        self.data['recovery_pubkey'] = recovery_pubkey
        self.run('choose_keystore')

    def on_three_keys_import(self, recovery_pubkey: str):
        self.data['recovery_pubkey'] = recovery_pubkey
        self.run('restore_from_seed')

    def choose_keystore(self):
        assert self.wallet_type in ['standard', 'multisig', '2-key', '3-key']
        i = len(self.keystores)
        title = _('Add cosigner') + ' (%d of %d)' % (i + 1, self.n) if self.wallet_type == 'multisig' else _('Keystore')
        base_choices = []
        advanced_choices = []
        if self.wallet_type == 'multisig' and i > 0:
            message = _('Add a cosigner to your multi-sig wallet')
            base_choices = [
                ('restore_from_key', _('Enter cosigner key')),
                ('restore_from_seed', _('Enter cosigner seed')),
            ]
            if not self.is_kivy:
                base_choices.append(('choose_hw_device', _('Cosign with hardware device')))
        else:
            message = _('Do you want to create a new seed or to restore a wallet using an existing seed?')
            run_next_method = 'choose_seed_type'
            # for 2- & 3- keys wallets we use only segwit type seed
            if self.wallet_type in ['2-key', '3-key']:
                run_next_method = 'create_segwit_seed'
            base_choices = [
                (run_next_method, _('Create a new seed')),
                ('restore_from_seed', _('I already have a seed')),
            ]
            advanced_choices = [
                ('restore_from_key', _('Use a master key')),
            ]
            if not self.is_kivy and self.wallet_type not in ['2-key', '3-key']:
                advanced_choices.append(('choose_hw_device', _('Use a hardware device')))
        if self.wallet_type == 'multisig':
            self.choice_dialog(title=title, message=message, choices=base_choices + advanced_choices, run_next=self.run)
        else:
            hint = ' '.join([
                '<b>',
                _('Seed phrase'),
                '</b><br/>',
                _("This is a list of words which store all the information necessary to restore the wallet. "
                  "Without the seed phrase, you won’t be able to access your funds in case of a technical "
                  "issue or if your device is stolen. ")
            ])

            self.choice_dialog_with_advanced_options(
                title=title, message=message, base_choices=base_choices, advanced_choices=advanced_choices,
                run_next=self.run, hint=hint
            )

    def import_addresses_or_keys(self):
        v = lambda x: keystore.is_address_list(x) or keystore.is_private_key_list(x, raise_on_error=True)
        title = _("Import BTCV addresses")
        message = _(
            "Enter a list of BTCV addresses (this will create a watching-only wallet) or a list of private keys.")
        self.add_xpub_dialog(title=title, message=message, run_next=self.on_import,
                             is_valid=v, allow_multi=True, show_wif_help=True)

    def on_import(self, text):
        # text is already sanitized by is_address_list and is_private_keys_list
        if keystore.is_address_list(text):
            self.data['addresses'] = {}
            for addr in text.split():
                assert bitcoin.is_address(addr)
                self.data['addresses'][addr] = {}
        elif keystore.is_private_key_list(text):
            self.data['addresses'] = {}
            k = keystore.Imported_KeyStore({})
            keys = keystore.get_private_keys(text)
            for pk in keys:
                assert bitcoin.is_private_key(pk)
                txin_type, pubkey = k.import_privkey(pk, None)
                addr = bitcoin.pubkey_to_address(txin_type, pubkey)
                self.data['addresses'][addr] = {'type': txin_type, 'pubkey': pubkey}
            self.keystores.append(k)
        else:
            return self.terminate()
        return self.run('create_wallet')

    def restore_from_key(self):
        if self.wallet_type == 'standard':
            v = keystore.is_master_key
            title = _("Create keystore from a master key")
            message = ' '.join([
                _("To create a watching-only wallet, please enter your master public key (xpub/ypub/zpub)."),
                _("To create a spending wallet, please enter a master private key (xprv/yprv/zprv).")
            ])
            self.add_xpub_dialog(title=title, message=message, run_next=self.on_restore_from_key, is_valid=v)
        else:
            i = len(self.keystores) + 1
            self.add_cosigner_dialog(index=i, run_next=self.on_restore_from_key, is_valid=keystore.is_bip32_key)

    def on_restore_from_key(self, text):
        k = keystore.from_master_key(text)
        self.on_keystore(k)

    def choose_hw_device(self, purpose=HWD_SETUP_NEW_WALLET, *, storage=None):
        title = _('Hardware Keystore')
        # check available plugins
        supported_plugins = self.plugins.get_hardware_support()
        devices = []  # type: List[Tuple[str, DeviceInfo]]
        devmgr = self.plugins.device_manager
        debug_msg = ''

        def failed_getting_device_infos(name, e):
            nonlocal debug_msg
            err_str_oneline = ' // '.join(str(e).splitlines())
            self.logger.warning(f'error getting device infos for {name}: {err_str_oneline}')
            indented_error_msg = '    '.join([''] + str(e).splitlines(keepends=True))
            debug_msg += f'  {name}: (error getting device infos)\n{indented_error_msg}\n'

        # scan devices
        try:
            scanned_devices = devmgr.scan_devices()
        except BaseException as e:
            self.logger.info('error scanning devices: {}'.format(repr(e)))
            debug_msg = '  {}:\n    {}'.format(_('Error scanning devices'), e)
        else:
            for splugin in supported_plugins:
                name, plugin = splugin.name, splugin.plugin
                # plugin init errored?
                if not plugin:
                    e = splugin.exception
                    indented_error_msg = '    '.join([''] + str(e).splitlines(keepends=True))
                    debug_msg += f'  {name}: (error during plugin init)\n'
                    debug_msg += '    {}\n'.format(_('You might have an incompatible library.'))
                    debug_msg += f'{indented_error_msg}\n'
                    continue
                # see if plugin recognizes 'scanned_devices'
                try:
                    # FIXME: side-effect: unpaired_device_info sets client.handler
                    device_infos = devmgr.unpaired_device_infos(None, plugin, devices=scanned_devices,
                                                                include_failing_clients=True)
                except HardwarePluginLibraryUnavailable as e:
                    failed_getting_device_infos(name, e)
                    continue
                except BaseException as e:
                    self.logger.exception('')
                    failed_getting_device_infos(name, e)
                    continue
                device_infos_failing = list(filter(lambda di: di.exception is not None, device_infos))
                for di in device_infos_failing:
                    failed_getting_device_infos(name, di.exception)
                device_infos_working = list(filter(lambda di: di.exception is None, device_infos))
                devices += list(map(lambda x: (name, x), device_infos_working))
        if not debug_msg:
            debug_msg = '  {}'.format(_('No exceptions encountered.'))
        if not devices:
            msg = (_('No hardware device detected.') + '\n' +
                   _('To trigger a rescan, press \'Next\'.') + '\n\n')
            if sys.platform == 'win32':
                msg += _('If your device is not detected on Windows, go to "Settings", "Devices", "Connected devices", '
                         'and do "Remove device". Then, plug your device again.') + '\n'
                msg += _('While this is less than ideal, it might help if you run Electrum as Administrator.') + '\n'
            else:
                msg += _('On Linux, you might have to add a new permission to your udev rules.') + '\n'
            msg += '\n\n'
            msg += _('Debug message') + '\n' + debug_msg
            self.confirm_dialog(title=title, message=msg,
                                run_next=lambda x: self.choose_hw_device(purpose, storage=storage))
            return
        # select device
        self.devices = devices
        choices = []
        for name, info in devices:
            state = _("initialized") if info.initialized else _("wiped")
            label = info.label or _("An unnamed {device_name}").format(device_name=name)
            try:
                transport_str = info.device.transport_ui_string[:20]
            except:
                transport_str = 'unknown transport'
            descr = f"{label} [{name}, {state}, {transport_str}]"
            choices.append(((name, info), descr))
        msg = _('Select a device') + ':'
        self.choice_dialog(title=title, message=msg, choices=choices,
                           run_next=lambda *args: self.on_device(*args, purpose=purpose, storage=storage))

    def on_device(self, name, device_info, *, purpose, storage=None):
        self.plugin = self.plugins.get_plugin(name)  # type: HW_PluginBase
        try:
            self.plugin.setup_device(device_info, self, purpose)
        except OSError as e:
            self.show_error(_('We encountered an error while connecting to your device:')
                            + '\n' + str(e) + '\n'
                            + _('To try to fix this, we will now re-pair with your device.') + '\n'
                            + _('Please try again.'))
            devmgr = self.plugins.device_manager
            devmgr.unpair_id(device_info.device.id_)
            self.choose_hw_device(purpose, storage=storage)
            return
        except OutdatedHwFirmwareException as e:
            if self.question(e.text_ignore_old_fw_and_continue(), title=_("Outdated device firmware")):
                self.plugin.set_ignore_outdated_fw()
                # will need to re-pair
                devmgr = self.plugins.device_manager
                devmgr.unpair_id(device_info.device.id_)
            self.choose_hw_device(purpose, storage=storage)
            return
        except (UserCancelled, GoBack):
            self.choose_hw_device(purpose, storage=storage)
            return
        except BaseException as e:
            self.logger.exception('')
            self.show_error(str(e))
            self.choose_hw_device(purpose, storage=storage)
            return
        if purpose == HWD_SETUP_NEW_WALLET:
            def f(derivation, script_type):
                derivation = normalize_bip32_derivation(derivation)
                self.run('on_hw_derivation', name, device_info, derivation, script_type)

            self.derivation_and_script_type_dialog(f)
        elif purpose == HWD_SETUP_DECRYPT_WALLET:
            derivation = get_derivation_used_for_hw_device_encryption()
            xpub = self.plugin.get_xpub(device_info.device.id_, derivation, 'standard', self)
            password = keystore.Xpub.get_pubkey_from_xpub(xpub, ())
            try:
                storage.decrypt(password)
            except InvalidPassword:
                # try to clear session so that user can type another passphrase
                devmgr = self.plugins.device_manager
                client = devmgr.client_by_id(device_info.device.id_)
                if hasattr(client, 'clear_session'):  # FIXME not all hw wallet plugins have this
                    client.clear_session()
                raise
        else:
            raise Exception('unknown purpose: %s' % purpose)

    def derivation_and_script_type_dialog(self, f):
        message1 = _('Choose the type of addresses in your wallet.')
        message2 = ' '.join([
            _('You can override the suggested derivation path.'),
            _('If you are not sure what this is, leave this field unchanged.')
        ])
        if self.wallet_type == 'multisig':
            # There is no general standard for HD multisig.
            # For legacy, this is partially compatible with BIP45; assumes index=0
            # For segwit, a custom path is used, as there is no standard at all.
            default_choice_idx = 2
            choices = [
                ('standard', 'legacy multisig - p2sh', normalize_bip32_derivation("m/45'/0")),
                ('p2wsh-p2sh', 'p2sh-segwit multisig - p2wsh-p2sh', purpose48_derivation(0, xtype='p2wsh-p2sh')),
                ('p2wsh', 'native segwit multisig - p2wsh', purpose48_derivation(0, xtype='p2wsh')),
            ]
        else:
            default_choice_idx = 2
            choices = [
                ('standard', 'legacy - p2pkh', bip44_derivation(0, bip43_purpose=44)),
                ('p2wpkh-p2sh', 'p2sh-segwit - p2wpkh-p2sh', bip44_derivation(0, bip43_purpose=49)),
                ('p2wpkh', 'native segwit - p2wpkh', bip44_derivation(0, bip43_purpose=84)),
            ]
        while True:
            try:
                self.choice_and_line_dialog(
                    run_next=f, title=_('Script type and Derivation path'), message1=message1,
                    message2=message2, choices=choices, test_text=is_bip32_derivation,
                    default_choice_idx=default_choice_idx)
                return
            except ScriptTypeNotSupported as e:
                self.show_error(e)
                # let the user choose again

    def on_hw_derivation(self, name, device_info, derivation, xtype):
        from .keystore import hardware_keystore
        try:
            xpub = self.plugin.get_xpub(device_info.device.id_, derivation, xtype, self)
            root_xpub = self.plugin.get_xpub(device_info.device.id_, 'm', 'standard', self)
        except ScriptTypeNotSupported:
            raise  # this is handled in derivation_dialog
        except BaseException as e:
            self.logger.exception('')
            self.show_error(e)
            return
        xfp = BIP32Node.from_xkey(root_xpub).calc_fingerprint_of_this_node().hex().lower()
        d = {
            'type': 'hardware',
            'hw_type': name,
            'derivation': derivation,
            'root_fingerprint': xfp,
            'xpub': xpub,
            'label': device_info.label,
        }
        k = hardware_keystore(d)
        self.on_keystore(k)

    def passphrase_dialog(self, run_next, is_restoring=False):
        title = _('Seed extension')
        message = '\n'.join([
            _('You may extend your seed with custom words.'),
            _('Your seed extension must be saved together with your seed.'),
        ])
        warning = '\n'.join([
            _('Note that this is NOT your encryption password.'),
            _('If you do not know what this is, leave this field empty.'),
        ])
        warn_issue4566 = is_restoring and self.seed_type == 'bip39'
        self.line_dialog(title=title, message=message, warning=warning,
                         default='', test=lambda x: True, run_next=run_next,
                         warn_issue4566=warn_issue4566)

    def restore_from_seed(self):
        self.opt_bip39 = True
        self.opt_ext = True
        is_cosigning_seed = lambda x: mnemonic.seed_type(x) in ['standard', 'segwit']
        test = mnemonic.is_seed if self.wallet_type in ['standard', '2-key', '3-key'] else is_cosigning_seed
        self.restore_seed_dialog(run_next=self.on_restore_seed, test=test)

    def on_restore_seed(self, seed, is_bip39, is_ext, is_gold_wallet_import=False):
        self.seed_type = 'bip39' if is_bip39 or is_gold_wallet_import else mnemonic.seed_type(seed)
        if self.seed_type == 'bip39':
            f = lambda passphrase: self.on_restore_bip39(seed, passphrase)
            if self.wallet_type in ['2-key', '3-key']:
                f = lambda passphrase: self.on_bip43(seed=seed, passphrase=passphrase,
                                                     derivation="m/0'", script_type='p2wpkh-p2sh')
            elif self.wallet_type == 'standard' and is_gold_wallet_import:
                f = lambda passphrase: self.on_bip43(seed=seed, passphrase=passphrase,
                                                     derivation="m/49'/440'/0'", script_type='p2wpkh-p2sh')

            self.passphrase_dialog(run_next=f, is_restoring=True) if is_ext else f('')
        elif self.seed_type in ['standard', 'segwit']:
            f = lambda passphrase: self.run('create_keystore', seed, passphrase)
            self.passphrase_dialog(run_next=f, is_restoring=True) if is_ext else f('')
        elif self.seed_type == 'old':
            self.run('create_keystore', seed, '')
        elif mnemonic.is_any_2fa_seed_type(self.seed_type):
            self.load_2fa()
            self.run('on_restore_seed', seed, is_ext)
        else:
            raise Exception('Unknown seed type', self.seed_type)

    def on_restore_bip39(self, seed, passphrase):
        def f(derivation, script_type):
            derivation = normalize_bip32_derivation(derivation)
            self.run('on_bip43', seed, passphrase, derivation, script_type)

        self.derivation_and_script_type_dialog(f)

    def create_keystore(self, seed, passphrase):
        k = keystore.from_seed(seed, passphrase, self.wallet_type == 'multisig')
        self.on_keystore(k)

    def on_bip43(self, seed, passphrase, derivation, script_type):
        k = keystore.from_bip39_seed(seed, passphrase, derivation, xtype=script_type)
        self.on_keystore(k)

    def on_keystore(self, k):
        has_xpub = isinstance(k, keystore.Xpub)
        if has_xpub:
            t1 = xpub_type(k.xpub)
        if self.wallet_type in ['standard', '2-key', '3-key']:
            if has_xpub and t1 not in ['standard', 'p2wpkh', 'p2wpkh-p2sh']:
                self.show_error(_('Wrong key type') + ' %s' % t1)
                self.run('choose_keystore')
                return
            self.keystores.append(k)
            self.run('create_wallet')
        elif self.wallet_type == 'multisig':
            assert has_xpub
            if t1 not in ['standard', 'p2wsh', 'p2wsh-p2sh']:
                self.show_error(_('Wrong key type') + ' %s' % t1)
                self.run('choose_keystore')
                return
            if k.xpub in map(lambda x: x.xpub, self.keystores):
                self.show_error(_('Error: duplicate master public key'))
                self.run('choose_keystore')
                return
            if len(self.keystores) > 0:
                t2 = xpub_type(self.keystores[0].xpub)
                if t1 != t2:
                    self.show_error(
                        _('Cannot add this cosigner:') + '\n' + "Their key type is '%s', we are '%s'" % (t1, t2))
                    self.run('choose_keystore')
                    return
            self.keystores.append(k)
            if len(self.keystores) == 1:
                xpub = k.get_master_public_key()
                self.reset_stack()
                self.run('show_xpub_and_add_cosigners', xpub)
            elif len(self.keystores) < self.n:
                self.run('choose_keystore')
            else:
                self.run('create_wallet')

    def create_wallet(self):
        encrypt_keystore = any(k.may_have_password() for k in self.keystores)
        # note: the following condition ("if") is duplicated logic from
        # wallet.get_available_storage_encryption_version()
        if self.wallet_type == 'standard' and isinstance(self.keystores[0], keystore.Hardware_KeyStore):
            # offer encrypting with a pw derived from the hw device
            k = self.keystores[0]
            try:
                k.handler = self.plugin.create_handler(self)
                password = k.get_password_for_storage_encryption()
            except UserCancelled:
                devmgr = self.plugins.device_manager
                devmgr.unpair_xpub(k.xpub)
                self.choose_hw_device()
                return
            except BaseException as e:
                self.logger.exception('')
                self.show_error(str(e))
                return
            self.request_storage_encryption(
                run_next=lambda encrypt_storage: self.on_password(
                    password,
                    encrypt_storage=encrypt_storage,
                    storage_enc_version=StorageEncryptionVersion.XPUB_PASSWORD,
                    encrypt_keystore=False))
        else:
            # reset stack to disable 'back' button in password dialog
            self.reset_stack()
            # prompt the user to set an arbitrary password
            self.request_password(
                run_next=lambda password, encrypt_storage: self.on_password(
                    password,
                    encrypt_storage=encrypt_storage,
                    storage_enc_version=StorageEncryptionVersion.USER_PASSWORD,
                    encrypt_keystore=encrypt_keystore),
                force_disable_encrypt_cb=not encrypt_keystore)

    def on_password(self, password, *, encrypt_storage: bool,
                    storage_enc_version=StorageEncryptionVersion.USER_PASSWORD,
                    encrypt_keystore: bool):
        for k in self.keystores:
            if k.may_have_password():
                k.update_password(None, password)
        if self.wallet_type in ['standard', '2-key', '3-key']:
            self.data['seed_type'] = self.seed_type
            keys = self.keystores[0].dump()
            self.data['keystore'] = keys
        elif self.wallet_type == 'multisig':
            for i, k in enumerate(self.keystores):
                self.data['x%d/' % (i + 1)] = k.dump()
        elif self.wallet_type == 'imported':
            if len(self.keystores) > 0:
                keys = self.keystores[0].dump()
                self.data['keystore'] = keys
        else:
            raise Exception('Unknown wallet type')
        self.pw_args = WizardWalletPasswordSetting(password=password,
                                                   encrypt_storage=encrypt_storage,
                                                   storage_enc_version=storage_enc_version,
                                                   encrypt_keystore=encrypt_keystore)
        self.terminate()

    def create_storage(self, path):
        if os.path.exists(path):
            raise Exception('file already exists at path')
        if not self.pw_args:
            return
        pw_args = self.pw_args
        self.pw_args = None  # clean-up so that it can get GC-ed
        storage = WalletStorage(path)
        storage.set_keystore_encryption(bool(pw_args.password) and pw_args.encrypt_keystore)
        if pw_args.encrypt_storage:
            storage.set_password(pw_args.password, enc_version=pw_args.storage_enc_version)
        for key, value in self.data.items():
            storage.put(key, value)
        storage.write()
        storage.load_plugins()
        return storage

    def terminate(self, *, storage: Optional[WalletStorage] = None):
        raise NotImplementedError()  # implemented by subclasses

    def show_xpub_and_add_cosigners(self, xpub):
        self.show_xpub_dialog(xpub=xpub, run_next=lambda x: self.run('choose_keystore'))

    def choose_seed_type(self, message=None, choices=None):
        title = _('Choose Seed type')
        if message is None:
            message = _("The type of addresses used by your wallet will depend on your seed.")

        if choices is None:
            base_choices = [
                ('create_segwit_seed', _('Segwit')),
            ]
            advanced_choices = [
                ('create_standard_seed', _('Legacy')),
            ]
        hint = ' '.join([
            _('Choose Seed type'),
            _("'Legacy' is the original address type, while 'Segwit' is the newer address format with lower fees."),
            "\n",
            _("Segwit wallets use bech32 addresses, defined in BIP173.")
        ])

        self.choice_dialog_with_advanced_options(
            title=title, message=message, base_choices=base_choices, advanced_choices=advanced_choices,
            run_next=self.run, hint=hint
        )

    def create_segwit_seed(self):
        self.create_seed('segwit')

    def create_standard_seed(self):
        self.create_seed('standard')

    def create_seed(self, seed_type):
        from . import mnemonic
        self.seed_type = seed_type
        seed = mnemonic.Mnemonic('en').make_seed(self.seed_type)
        self.opt_bip39 = False
        f = lambda x: self.request_passphrase(seed, x)
        self.show_seed_dialog(run_next=f, seed_text=seed)

    def request_passphrase(self, seed, opt_passphrase):
        if opt_passphrase:
            f = lambda x: self.confirm_seed(seed, x)
            self.passphrase_dialog(run_next=f)
        else:
            self.run('confirm_seed', seed, '')

    def confirm_seed(self, seed, passphrase):
        f = lambda x: self.confirm_passphrase(seed, passphrase)
        self.confirm_seed_dialog(run_next=f, test=lambda x: x == seed)

    def confirm_passphrase(self, seed, passphrase):
        f = lambda x: self.run('create_keystore', seed, x)
        if passphrase:
            title = _('Confirm Seed Extension')
            message = '\n'.join([
                _('Your seed extension must be saved together with your seed.'),
                _('Please type it here.'),
            ])
            self.line_dialog(run_next=f, title=title, message=message, default='', test=lambda x: x == passphrase)
        else:
            f('')
