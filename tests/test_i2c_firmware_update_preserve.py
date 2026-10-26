# SPDX-License-Identifier: Apache-2.0
import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"examples"))
import i2c_firmware_update_preserve as fw


class Receiver:
    def __init__(self, version="v00.144", normal_after_ram=False, failed_slot=None,
                 failed_frame=None, force_response=b"\0", ram_rejection=None,
                 boot_timeout=False, final_version="v00.201"):
        self.version = version
        self.normal_after_ram = normal_after_ram
        self.failed_slot = failed_slot
        self.failed_frame = failed_frame
        self.force_response = force_response
        self.ram_rejection = ram_rejection
        self.boot_timeout = boot_timeout
        self.final_version = final_version
        self.ram = False
        self.booted = False
        self.calls = []

    def exchange(self, opcode, payload, timeout):
        self.calls.append((opcode, payload))
        if not fw.allowed_command(opcode,payload):
            raise AssertionError("Forbidden command reached receiver")
        if opcode == 6:
            if self.ram and not self.booted and not self.normal_after_ram:
                if self.ram_rejection:
                    raise fw.ReceiverCommandError(6,self.ram_rejection)
                raise TimeoutError("RAM updater does not implement normal VER")
            version = self.final_version if self.booted else self.version
            return b"\0"+version.encode()+b"\r\n"
        if (opcode,payload)==(0,b"\xff"):
            return self.force_response
        if opcode==1 and struct.unpack_from("<HH",payload)[1]==self.failed_frame:
            raise TimeoutError("Missing firmware frame ACK")
        if opcode==2:
            self.ram=True
        if opcode==9 and payload[0]==self.failed_slot:
            return b"\xea"
        if (opcode,payload)==(0,b"\x01"):
            self.booted=True
            if self.boot_timeout:
                raise TimeoutError("Missing boot ACK")
            return b"\x01"
        return b"\0"


class PreservingUpdateTests(unittest.TestCase):
    def setUp(self):
        self.images=[("cxd5610_updater.efpk",b"u"*4100,None),
                     ("cxd5610_sdk.efpk",b"s"*8200,0),
                     ("cxd5610_app.efpk",b"a"*4100,1),
                     ("cxd5610_lib.efpk",b"l",2)]
        self.specs=tuple((n,len(d),hashlib.sha256(d).hexdigest(),k) for n,d,k in self.images)
        self.profiles={"test-v201":("v00.201",self.specs)}

    def run_update(self, receiver, images=None, **kwargs):
        with patch.object(fw,"PROFILES",self.profiles):
            return fw.run_update(receiver,self.images if images is None else images,
                                 "test-v201","v00.144",lambda _:None,settle=lambda _:None,**kwargs)

    def test_only_exact_ordered_profile_is_accepted(self):
        for images in [self.images+[("board.cfg",b"c",3)],self.images[::-1],
                       [self.images[0],self.images[1],("cxd5610_app.efpk",b"x"*4100,1),self.images[3]]]:
            receiver=Receiver()
            with self.assertRaises(ValueError):
                self.run_update(receiver,images)
            self.assertEqual(receiver.calls,[])

    def test_success_programs_only_sdk_app_lib_and_checks_target(self):
        receiver=Receiver()
        self.assertEqual(self.run_update(receiver),"v00.201")
        self.assertEqual([p for op,p in receiver.calls if op==9],[b"\0\0\0",b"\x01\0\0",b"\x02\0\0"])
        self.assertNotIn((0,b"\0"),receiver.calls)
        self.assertEqual(receiver.calls[-1],(6,b""))

    def test_initial_version_mismatch_blocks_stop_force_and_transfer(self):
        receiver=Receiver(version="v00.164")
        with self.assertRaisesRegex(ValueError,"no update started"):
            self.run_update(receiver)
        self.assertEqual(receiver.calls,[(6,b"")])

    def test_already_target_does_not_reflash(self):
        receiver=Receiver(version="v00.201")
        self.assertEqual(self.run_update(receiver),"v00.201")
        self.assertEqual(receiver.calls,[(6,b"")])

    def test_force_rejection_never_falls_back_to_erase(self):
        receiver=Receiver(force_response=b"\x01")
        with self.assertRaisesRegex(ValueError,"rejected"):
            self.run_update(receiver)
        self.assertEqual(receiver.calls[-1],(0,b"\xff"))
        self.assertFalse(any(op in (1,2,9) for op,_ in receiver.calls))

    def test_explicit_non_erasing_reboot_precedes_force_entry(self):
        class RebootReceiver(Receiver):
            def exchange(self,opcode,payload,timeout):
                if (opcode,payload)==(0,b"\x01") and not self.ram:
                    self.calls.append((opcode,payload))
                    return b"\x01"
                return super().exchange(opcode,payload,timeout)
        receiver=RebootReceiver()
        self.assertEqual(self.run_update(receiver,reboot_before_force=True),"v00.201")
        self.assertEqual(receiver.calls[:4],[(6,b""),(0x31,b""),(0,b"\x01"),(0,b"\xff")])
        self.assertNotIn((0,b"\0"),receiver.calls)

    def test_rejected_reboot_never_sends_force_or_code(self):
        receiver=Receiver()
        exchange=receiver.exchange
        def rejected(opcode,payload,timeout):
            if (opcode,payload)==(0,b"\x01"):
                receiver.calls.append((opcode,payload))
                return b"\xea"
            return exchange(opcode,payload,timeout)
        receiver.exchange=rejected
        with self.assertRaisesRegex(ValueError,"rejected"):
            self.run_update(receiver,reboot_before_force=True)
        self.assertNotIn((0,b"\xff"),receiver.calls)
        self.assertFalse(any(op in (1,2,9) for op,_ in receiver.calls))

    def test_reset_entry_repeats_only_force_until_receiver_accepts(self):
        receiver=Receiver()
        exchange=receiver.exchange
        replies=iter([b"\x01",b"\xea",fw.CommandWriteError(121,"reset asserted"),
                      fw.CommandWriteError(5,"reset asserted"),
                      fw.ReceiverCommandError(0,-2),b"\0"])
        def entry(opcode,payload,timeout):
            if (opcode,payload)==(0,b"\xff"):
                receiver.calls.append((opcode,payload))
                reply=next(replies)
                if isinstance(reply,Exception):
                    raise reply
                return reply
            return exchange(opcode,payload,timeout)
        receiver.exchange=entry
        self.assertEqual(self.run_update(receiver,reset_wait=2),"v00.201")
        self.assertEqual(receiver.calls[2:8],[(0,b"\xff")]*6)
        self.assertEqual(receiver.calls[8][0],1)
        self.assertNotIn((0,b"\0"),receiver.calls)

    def test_reset_entry_does_not_retry_timeouts_partial_reads_or_other_errors(self):
        for error in (TimeoutError("IRQ"),OSError(121,"body read failed"),
                      OSError(5,"body read failed"),
                      OSError("Short write"),ValueError("Invalid header"),
                      fw.ReceiverCommandError(6,-2),fw.ReceiverCommandError(0,-5)):
            receiver=Receiver()
            exchange=receiver.exchange
            def fail(opcode,payload,timeout):
                if (opcode,payload)==(0,b"\xff"):
                    receiver.calls.append((opcode,payload))
                    raise error
                return exchange(opcode,payload,timeout)
            receiver.exchange=fail
            with self.assertRaises(type(error)):
                self.run_update(receiver,reset_wait=2)
            self.assertEqual(receiver.calls,[(6,b""),(0x31,b""),(0,b"\xff")])

    def test_expired_reset_window_sends_no_image(self):
        receiver=Receiver()
        with patch.object(fw.time,"monotonic",side_effect=[10,12]):
            with self.assertRaisesRegex(TimeoutError,"no image sent"):
                self.run_update(receiver,reset_wait=1)
        self.assertEqual(receiver.calls,[(6,b""),(0x31,b"")])

    def test_ram_transfer_timeout_does_not_execute_or_program(self):
        receiver=Receiver(failed_frame=2)
        with self.assertRaises(TimeoutError):
            self.run_update(receiver)
        self.assertEqual([struct.unpack_from("<HH",p)[1] for op,p in receiver.calls if op==1],[1,2])
        self.assertFalse(any(op in (2,9) for op,_ in receiver.calls))

    def test_image_write_eio_is_not_retried_after_force_entry(self):
        receiver=Receiver()
        exchange=receiver.exchange
        def fail(opcode,payload,timeout):
            if opcode==1:
                receiver.calls.append((opcode,payload))
                raise fw.CommandWriteError(5,"image transfer failed")
            return exchange(opcode,payload,timeout)
        receiver.exchange=fail
        with self.assertRaises(fw.CommandWriteError):
            self.run_update(receiver,reset_wait=2)
        self.assertEqual(sum(op==1 for op,_ in receiver.calls),1)
        self.assertFalse(any(op in (2,9) for op,_ in receiver.calls))

    def test_normal_firmware_after_ram_blocks_nonvolatile_writes(self):
        receiver=Receiver(normal_after_ram=True)
        with self.assertRaisesRegex(RuntimeError,"still responds"):
            self.run_update(receiver)
        self.assertFalse(any(op==9 for op,_ in receiver.calls))
        self.assertNotIn((0,b"\x01"),receiver.calls)

    def test_only_known_ram_version_rejection_is_accepted(self):
        self.assertEqual(self.run_update(Receiver(ram_rejection=-2)),"v00.201")
        receiver=Receiver(ram_rejection=-22)
        with self.assertRaises(fw.ReceiverCommandError):
            self.run_update(receiver)
        self.assertFalse(any(op==9 for op,_ in receiver.calls))

    def test_write_rejection_stops_without_retry_later_images_or_reset(self):
        receiver=Receiver(failed_slot=1)
        with self.assertRaisesRegex(ValueError,"rejected"):
            self.run_update(receiver)
        self.assertEqual([p[0] for op,p in receiver.calls if op==9],[0,1])
        self.assertEqual(receiver.calls[-1],(9,b"\x01\0\0"))

    def test_boot_ack_loss_requires_correct_independent_version(self):
        self.assertEqual(self.run_update(Receiver(boot_timeout=True)),"v00.201")
        with self.assertRaisesRegex(ValueError,"Expected target"):
            self.run_update(Receiver(boot_timeout=True,final_version="v00.144"))

    def test_transmission_guard_rejects_erase_cfg_and_settings_before_io(self):
        transport=object.__new__(fw.PreservingI2CTransport)
        forbidden=[(0,b"\0"),(9,b"\x03\x01\0"),(9,b"\x03\0\0"),
                   (9,b"\x01\x01\0"),(0x10,b""),(0x32,b"\xef\x3f"),
                   (1,b"\x01\0\0\0x"),(1,b"\x01\0\x02\0x")]
        with patch.object(fw.os,"write") as write:
            for op,payload in forbidden:
                with self.assertRaisesRegex(ValueError,"forbidden"):
                    transport.exchange(op,payload,1)
            write.assert_not_called()

    def test_whole_packet_single_write_and_no_short_write_retry(self):
        transport=object.__new__(fw.PreservingI2CTransport)
        transport.fd=9;transport.emit=lambda _:None
        transport.read_packet=lambda deadline:(0,b"\0")
        with patch.object(fw.os,"write",side_effect=lambda fd,data:len(data)) as write:
            self.assertEqual(transport.exchange(0,b"\xff",1),b"\0")
            write.assert_called_once_with(9,bytes.fromhex("7f01000080ffff"))
        with patch.object(fw.os,"write",return_value=0) as write:
            with self.assertRaisesRegex(OSError,"no retry"):
                transport.exchange(0,b"\xff",1)
            self.assertEqual(write.call_count,1)

    def test_only_command_write_nack_or_eio_is_marked_as_retryable(self):
        transport=object.__new__(fw.PreservingI2CTransport)
        transport.fd=9;transport.emit=lambda _:None
        for code in (5,6,121):
            with patch.object(fw.os,"write",side_effect=OSError(code,"write failed")):
                with self.assertRaises(fw.CommandWriteError):
                    transport.exchange(0,b"\xff",1)
            def read_error(deadline):
                raise OSError(code,"read failed")
            transport.read_packet=read_error
            with patch.object(fw.os,"write",side_effect=lambda fd,data:len(data)):
                with self.assertRaises(OSError) as caught:
                    transport.exchange(0,b"\xff",1)
            self.assertNotIsInstance(caught.exception,fw.CommandWriteError)

    def test_command_error_is_fully_consumed_and_identified(self):
        transport=object.__new__(fw.PreservingI2CTransport)
        transport.fd=9;transport.emit=lambda _:None
        transport.read_packet=lambda deadline:(0xff,b"\x06\xfe")
        with patch.object(fw.os,"write",side_effect=lambda fd,data:len(data)):
            with self.assertRaises(fw.ReceiverCommandError) as caught:
                transport.exchange(6,b"",1)
        self.assertEqual((caught.exception.opcode,caught.exception.code),(6,-2))

    def write_inputs(self,directory):
        for name,data,_ in self.images:
            (directory/name).write_bytes(data)

    def test_loader_rejects_cfg_mixed_hashes_and_wrong_version(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(fw,"PROFILES",self.profiles):
            directory=Path(tmp);self.write_inputs(directory)
            self.assertEqual(fw.load_images(directory,"test-v201"),self.images)
            cfg=directory/"other_board.cfg";cfg.write_bytes(b"config")
            with self.assertRaisesRegex(ValueError,"CFG"):
                fw.load_images(directory,"test-v201")
            cfg.unlink();(directory/"FW_VERSION").write_text("v00.164")
            with self.assertRaisesRegex(ValueError,"FW_VERSION"):
                fw.load_images(directory,"test-v201")
            (directory/"FW_VERSION").unlink()
            (directory/"cxd5610_sdk.efpk").write_bytes(b"x"*8200)
            with self.assertRaisesRegex(ValueError,"hash mismatch"):
                fw.load_images(directory,"test-v201")

    def test_offline_plan_never_constructs_a_device(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(fw,"PROFILES",self.profiles):
            root=Path(tmp);images=root/"images";images.mkdir();self.write_inputs(images)
            argv=["update","--profile","test-v201","--image-dir",str(images),"--log-dir",str(root/"plan")]
            with patch.object(sys,"argv",argv),patch.object(fw,"PreservingI2CTransport",side_effect=AssertionError("Device access during offline plan")),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fw.main(),0)
            summary=json.loads((root/"plan/summary.json").read_text())
            self.assertFalse(summary['device_opened'])
            self.assertEqual(summary['planned_program_operands'],['000000','010000','020000'])


if __name__=="__main__":
    unittest.main()
