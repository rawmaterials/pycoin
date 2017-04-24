import struct

from .flags import VERIFY_MINIMALDATA

from ...intbytes import from_bytes

from . import ScriptError
from . import errno
from . import opcodes
from .instruction_lookup import make_instruction_lookup
from .ConditionalStack import ConditionalStack
from .ScriptCodec import ScriptCodec
from .IntStreamer import IntStreamer
from .Stack import Stack


class VM(object):
    MAX_SCRIPT_LENGTH = 10000
    MAX_BLOB_LENGTH = 520
    MAX_OP_COUNT = 201
    MAX_STACK_SIZE = 1000
    OPCODE_LIST = opcodes.OPCODE_LIST

    VM_FALSE = IntStreamer.int_to_script_bytes(0)
    VM_TRUE = IntStreamer.int_to_script_bytes(1)

    ConditionalStack = ConditionalStack
    Stack = Stack
    IntStreamer = IntStreamer

    @classmethod
    def nonnegative_int_from_script_bytes(class_, b, require_minimal):
        v = class_.IntStreamer.int_from_script_bytes(b, require_minimal=require_minimal)
        if v < 0:
            raise ScriptError("unexpectedly got negative value", errno.INVALID_STACK_OPERATION)
        return v

    @classmethod
    def bool_from_script_bytes(class_, v, require_minimal=False):
        v = class_.IntStreamer.int_from_script_bytes(v, require_minimal=require_minimal)
        if require_minimal:
            if v not in (0, 1):
                raise ScriptError("non-minimally encoded", errno.UNKNOWN_ERROR)
        return bool(v)

    @classmethod
    def bool_to_script_bytes(class_, v):
        return class_.VM_TRUE if v else class_.VM_FALSE

    @classmethod
    def get_opcodes(class_, script, verify_minimal_data=False, pc=0):
        pc = 0
        while pc < len(script):
            opcode, data, new_pc = class_.ScriptCodec.get_opcode(script, pc, verify_minimal_data=verify_minimal_data)
            yield opcode, data, pc, new_pc
            pc = new_pc

    def eval_script(self, script, tx_context, vm_context, initial_stack=None):
        if len(script) > self.MAX_SCRIPT_LENGTH:
            raise ScriptError("script too long", errno.SCRIPT_SIZE)

        self.pc = 0
        self.tx_context = tx_context
        self.stack = initial_stack or self.Stack()
        self.script = script
        self.altstack = self.Stack()
        self.conditional_stack = self.ConditionalStack()
        self.op_count = 0
        self.begin_code_hash = 0
        self.flags = vm_context.flags
        self.traceback_f = vm_context.traceback_f
        self.signature_for_hash_type_f = vm_context.signature_for_hash_type_f

        while self.pc < len(self.script):
            self.eval_instruction()

        self.post_script_check()
        return self.stack

    def eval_instruction(self):
        all_if_true = self.conditional_stack.all_if_true()

        # don't actually check for minimal data unless data will be pushed onto the stack
        verify_minimal_data = self.flags & VERIFY_MINIMALDATA and all_if_true
        opcode, data, pc = self.ScriptCodec.get_opcode(self.script, self.pc, verify_minimal_data=verify_minimal_data)
        if data and len(data) > self.MAX_BLOB_LENGTH:
            raise ScriptError("pushing too much data onto stack", errno.PUSH_SIZE)

        if data is None:
            self.op_count += 1

        self.check_stack_size()

        f = self.INSTRUCTION_LOOKUP[opcode]
        if self.traceback_f:
            f = self.traceback_f(opcode, data, pc, self) or f

        if data is not None and all_if_true:
            self.stack.append(data)

        if all_if_true or getattr(f, "outside_conditional", False):
            f(self)

        self.pc = pc

        if self.op_count > self.MAX_OP_COUNT:
            raise ScriptError("script contains too many operations", errno.OP_COUNT)

    def check_stack_size(self):
        if len(self.stack) + len(self.altstack) > self.MAX_STACK_SIZE:
            raise ScriptError("stack has > %d items" % self.MAX_STACK_SIZE, errno.STACK_SIZE)

    def post_script_check(self):
        self.conditional_stack.check_final_state()
        self.check_stack_size()

    @classmethod
    def delete_subscript(class_, script, subscript):
        """
        Returns a script with the given subscript removed. The subscript
        must appear in the main script aligned to opcode boundaries for it
        to be removed.
        """
        new_script = bytearray()
        pc = 0
        for opcode, data, pc, new_pc in class_.get_opcodes(script):
            section = script[pc:new_pc]
            if section != subscript:
                new_script.extend(section)
        return bytes(new_script)


# BRAIN DAMAGE BELOW HERE

def build_microcode(class_):
    class_.INSTRUCTION_LOOKUP = make_instruction_lookup(class_.OPCODE_LIST)


def make_variable_decoder(dec_length):
    def decode_OP_PUSHDATA(script, pc):
        pc += 1
        size_blob = script[pc:pc+dec_length]
        if len(size_blob) < dec_length:
            raise ScriptError("unexpected end of data when size expected", errno.BAD_OPCODE)
        size = from_bytes(size_blob, byteorder="little")
        pc += dec_length
        return size, pc
    return decode_OP_PUSHDATA


OPCODE_CONST_LIST = [("OP_%d" % i, IntStreamer.int_to_script_bytes(i)) for i in range(17)] + [
    ("OP_1NEGATE", IntStreamer.int_to_script_bytes(-1))]
OPCODE_SIZED_LIST = [("OP_PUSH_%d" % i, i) for i in range(1, 76)]
OPCODE_VARIABLE_LIST = [
    ("OP_PUSHDATA1", 0, (1 << 8)-1, lambda d: struct.pack("<B", d), make_variable_decoder(1)),
    ("OP_PUSHDATA2", (1 << 8)-1, (1 << 16)-1, lambda d: struct.pack("<H", d), make_variable_decoder(2)),
    ("OP_PUSHDATA4", (1 << 16)-1, (1 << 32)-1, lambda d: struct.pack("<L", d), make_variable_decoder(4)),
]

OPCODE_LOOKUP = dict(o for o in opcodes.OPCODE_LIST)

build_microcode(VM)

VM.ScriptCodec = ScriptCodec(
    OPCODE_CONST_LIST, OPCODE_SIZED_LIST, OPCODE_VARIABLE_LIST, OPCODE_LOOKUP)

from .ScriptTools import ScriptTools

ScriptTools = ScriptTools(opcodes.OPCODE_LIST, IntStreamer, VM.ScriptCodec)

VM.bin_script = ScriptTools.compile_push_data_list
