import sys

from binaryninja import (
    BinaryReader, BinaryWriter,
    RegisterValueType, enums
)
from .sym_visitor import SymbolicVisitor
from .sym_state import State
from .utility.bninja_util import (
    get_imported_functions_and_addresses,
    find_os
)
from .utility.expr_wrap_util import symbolic
from .arch.arch_x86 import x86Arch
from .arch.arch_x86_64 import x8664Arch
from .arch.arch_armv7 import ArmV7Arch
from .utility import exceptions
from .expr import BVV, BVS
from .utility.binary_ninja_cache import BNCache
from .memory.sym_memory import InitData
from .multipath.fringe import Fringe

NO_COLOR = enums.HighlightStandardColor(0)
CURR_STATE_COLOR = enums.HighlightStandardColor.GreenHighlightColor
DEFERRED_STATE_COLOR = enums.HighlightStandardColor.RedHighlightColor
ERRORED_STATE_COLOR = enums.HighlightStandardColor.BlackHighlightColor


def find_arch(view):
    if view.arch.name == "x86":
        return x86Arch()
    elif view.arch.name == "x86_64":
        return x8664Arch()
    elif view.arch.name == "armv7":
        return ArmV7Arch()

    raise exceptions.UnsupportedArch(view.arch.name)


class SymbolicExecutor(object):
    def __init__(self, view, addr):

        self.view = view
        self.bw = BinaryWriter(view)
        self.br = BinaryReader(view)
        self.visitor = SymbolicVisitor(self)
        self.bncache = BNCache(view)
        self.vars = set()
        self.fringe = Fringe()
        self.ip = addr
        self.llil_ip = None
        self.arch = None
        self.user_hooks = dict()
        self.user_loggers = dict()
        self.imported_functions, self.imported_addresses = \
            get_imported_functions_and_addresses(view)
        self._last_colored_ip = None
        self._last_error = None
        self.init_with_zero = self.bncache.get_setting(
            "init_reg_mem_with_zero") == "true"

        self._wasjmp = False

        self.arch = find_arch(self.view)
        page_size = int(self.bncache.get_setting("memory.page_size"))
        self.state = State(self, arch=self.arch,
                           os=find_os(view), page_size=page_size)

        # load memory
        print("loading segments...")
        for segment in self.view.segments:
            start = segment.start
            end = segment.end
            size = segment.data_length
            print(segment, hex(start), "->", hex(size))

            if size == 0 and end - start != 0:
                size = end - start
                data = b"\x00" * size
            elif size == 0:
                continue
            else:
                self.br.seek(start)
                data = self.br.read(end-start)

            self.state.mem.mmap(
                self.state.address_page_aligned(start),
                self.state.address_page_aligned(end + self.state.mem.page_size - 1) -
                self.state.address_page_aligned(start),
                InitData(data, start - self.state.address_page_aligned(start))
            )
        print("loading finished!")

        current_function = self.bncache.get_function(addr)

        # initialize stack

        stack_page_size = int(self.bncache.get_setting("stack_size"))

        unmapped_page_init = self.state.mem.get_unmapped(
            stack_page_size,
            start_from=(0x80 << (self.arch.bits() - 8)))
        self.state.mem.mmap(
            unmapped_page_init*self.state.page_size,
            self.state.page_size * stack_page_size)
        # leave one page for upper stack portion
        p = unmapped_page_init + stack_page_size - 1
        stack_base = p * self.state.page_size - self.arch.bits() // 8

        self.state.initialize_stack(stack_base)

        # initialize registers
        for reg in self.arch.regs_data():
            reg_dict = self.arch.regs_data()[reg]
            val = current_function.get_reg_value_after(addr, reg)

            if val.type.value == RegisterValueType.StackFrameOffset:
                setattr(self.state.regs, reg, BVV(
                    stack_base + val.offset, reg_dict['size'] * 8))
            elif (
                val.type.value == RegisterValueType.ConstantPointerValue or
                val.type.value == RegisterValueType.ConstantValue
            ):
                setattr(self.state.regs, reg, BVV(
                    val.value, reg_dict['size'] * 8))
            else:
                if not self.init_with_zero:
                    symb = BVS(reg + "_init", reg_dict['size'] * 8)
                    self.vars.add(symb)
                    setattr(self.state.regs, reg, symb)
                else:
                    setattr(self.state.regs, reg, BVV(0, reg_dict['size'] * 8))

        # initialize known local variables
        stack_vars = current_function.stack_layout
        for var in stack_vars:
            offset = var.storage
            s_type = var.type

            if abs(offset) > self.state.page_size * (stack_page_size - 1):
                print("ERROR: not enough space in stack. Increase stack size")
                raise Exception(
                    "Not enough space in stack. Increase stack size")

            if s_type.confidence != 255:
                continue

            width = s_type.width
            name = var.name
            val = current_function.get_stack_contents_at(addr, offset, width)
            if val.type.value == RegisterValueType.StackFrameOffset:
                assert width*8 == self.arch.bits()  # has to happen... right?
                self.state.mem.store(
                    BVV(stack_base + offset, self.arch.bits()),
                    BVV(stack_base + val.offset, width*8),
                    endness=self.arch.endness())
            elif (
                val.type.value == RegisterValueType.ConstantPointerValue or
                val.type.value == RegisterValueType.ConstantValue
            ):
                self.state.mem.store(
                    BVV(stack_base + offset, self.arch.bits()),
                    BVV(val.value, width*8),
                    endness=self.arch.endness())
            elif not self.init_with_zero:
                symb = BVS(name + "_init", self.arch.bits())
                self.vars.add(symb)
                self.state.mem.store(
                    BVV(stack_base + offset, self.arch.bits()),
                    symb,
                    endness=self.arch.endness())

        # set eip
        self.state.set_ip(addr)
        self.llil_ip = current_function.llil.get_instruction_start(addr)

    def __str__(self):
        return "<SymExecutor id: 0x%x, %d states>" % \
            (id(self), self.fringe.num_states + 1 if self.state is not None else 0)

    def __repr__(self):
        return self.__str__()

    def put_in_deferred(self, state):
        self.fringe.add_deferred(state)

    def put_in_exited(self, state):
        self.fringe.add_exited(state)

    def put_in_unsat(self, state):
        save_unsat = self.bncache.get_setting("save_unsat") == 'true'
        if save_unsat:
            self.fringe.add_unsat(state)

    def put_in_errored(self, state, msg: str):
        self.fringe.add_errored(
            (msg, state)
        )

    def delete_comment_for_address(self, address):
        # TODO write an UI manager, this does not belong to the executor
        func = self.bncache.get_function(address)
        func.set_comment_at(address, None)

    def set_colors(self, reset=False):
        # TODO write an UI manager, this does not belong to the executor
        old_ip = self._last_colored_ip
        if old_ip is not None:
            old_func = self.bncache.get_function(old_ip)
            old_func.set_auto_instr_highlight(old_ip, NO_COLOR)

        for ip in self.fringe._deferred:
            func = self.bncache.get_function(ip)
            func.set_auto_instr_highlight(
                ip, DEFERRED_STATE_COLOR if not reset else NO_COLOR)
            if reset:
                func.set_comment_at(ip, None)
            elif len(self.fringe._deferred[ip]) > 1 or (len(self.fringe._deferred[ip]) == 1 and self.ip == ip):
                func.set_comment_at(ip, "n deferred: %d" %
                                    len(self.fringe._deferred[ip]))

        for _, state in self.fringe.errored:
            func = self.bncache.get_function(state.get_ip())
            func.set_auto_instr_highlight(
                state.get_ip(), ERRORED_STATE_COLOR if not reset else NO_COLOR)

        if self.state:
            func = self.bncache.get_function(self.ip)
            func.set_auto_instr_highlight(
                self.ip, CURR_STATE_COLOR if not reset else NO_COLOR)
        if not reset:
            self._last_colored_ip = self.ip

    def reset(self):
        self.set_colors(reset=True)

    def extract_mergeable_with_current_state(self, to_merge):
        # returns the set of states that do not deviate from
        # the current state after executing the current instruction

        func_name = self.bncache.get_function_name(self.ip)
        expr = self.bncache.get_llil(func_name, self.llil_ip)

        if expr.operation.name in {"LLIL_JUMP", "LLIL_JUMP_TO", "LLIL_CALL", "LLIL_TAILCALL"}:
            curr_state_dst = self.visitor.visit(expr.dest)
            if symbolic(curr_state_dst):
                # I do not want to call the solver... Just return them all
                return to_merge, list()

            curr_state = self.state

            mergeable = list()
            not_mergeable = list()
            for s in to_merge:
                self.state = s
                s_dst = self.visitor.visit(expr.dest)
                if symbolic(s_dst) or s_dst.value == curr_state_dst.value:
                    mergeable.append(s)
                else:
                    not_mergeable.append(s)

            self.state = curr_state
            return mergeable, not_mergeable

        return to_merge, list()

    def set_current_state(self, state):
        if self.state is not None:
            self.state.llil_ip = self.llil_ip
            self.put_in_deferred(self.state)
            self.state = None

        ip = state.get_ip()
        llil_ip = state.llil_ip

        self.state = state
        new_func = self.bncache.get_function(ip)
        self.ip = ip
        self.llil_ip = new_func.llil.get_instruction_start(
            ip) if llil_ip is None else llil_ip

    def select_from_deferred(self):
        if self.fringe.is_empty():
            return False

        state = self.fringe.get_one_deferred()
        self.set_current_state(state)
        return True

    def update_ip(self, funcion_name, new_llil_ip):
        self.llil_ip = new_llil_ip
        self.ip = self.bncache.get_address(funcion_name, new_llil_ip)
        self.state.set_ip(self.ip)
        self.state.llil_ip = new_llil_ip

    def _update_state_history(self, state, addr):
        if self.bncache.get_setting("save_state_history") == 'true':
            state.insn_history.add(addr)

    def _execute_one(self):
        self._last_error = None
        func_name = self.bncache.get_function_name(self.ip)

        # handle user hooks and loggers
        if self.ip in self.user_loggers:
            self.user_loggers[self.ip](self.state)
        if self.ip in self.user_hooks:
            old_ip = self.ip
            new_state, new_deferred, new_errored = self.user_hooks[self.ip](
                self.state)

            for s in new_deferred:
                self._update_state_history(s, old_ip)
                self.put_in_deferred(s)
            for s, msg in new_errored:
                self._update_state_history(s, old_ip)
                self.put_in_errored(s, msg)

            if new_state is not None:
                self.state = new_state

                if old_ip == self.state.get_ip():
                    new_ip = self.ip + \
                        self.bncache.get_instruction_len(self.ip)
                else:
                    new_ip = self.state.get_ip()

                dest_func_name = self.bncache.get_function_name(
                    new_ip
                )
                self.update_ip(
                    dest_func_name,
                    self.bncache.get_llil_address(dest_func_name, new_ip)
                )
                self._update_state_history(new_state, old_ip)
                return self.ip

        else:
            # check if a special handler is defined

            dont_use_special_handlers = \
                self.bncache.get_setting("dont_use_special_handlers") == 'true'
            disasm_str = self.bncache.get_disasm(self.ip)
            old_ip = self.ip

            try:
                if (
                    dont_use_special_handlers or
                    not self.arch.execute_special_handler(disasm_str, self)
                ):
                    expr = self.bncache.get_llil(func_name, self.llil_ip)
                    self.visitor.visit(expr)
                else:
                    self._wasjmp = True
                    self.ip = self.ip + \
                        self.view.get_instruction_length(self.ip)
                    self.state.set_ip(self.ip)
                    self.llil_ip = self.bncache.get_function(
                        self.ip).llil.get_instruction_start(self.ip)
            except exceptions.ExitException:
                self._update_state_history(self.state, old_ip)
                self.put_in_exited(self.state)
                self.state = None
            except exceptions.SENinjaError as err:
                sys.stderr.write("An error occurred: %s\n" % err.message)
                self.put_in_errored(self.state, str(err))
                self.state = None
                self._last_error = err
                if err.is_fatal():
                    raise err

            if self.state is not None:
                self._update_state_history(self.state, old_ip)

        if self.state is None:
            if self.fringe.is_empty():
                print("WARNING: no more states")
                return -1
            else:
                self.select_from_deferred()
                self._wasjmp = True

        if not self._wasjmp:
            # go on by 1 instruction
            self.update_ip(func_name, self.llil_ip + 1)
        else:
            self._wasjmp = False

        return self.ip

    def execute_one(self):
        if not self.state:
            return

        res = None
        try:
            single_llil_step = self.bncache.get_setting(
                "single_llil_step") == 'true'
            if single_llil_step:
                res = self._execute_one()
            else:
                old_ip = self.ip
                res = old_ip
                while res == old_ip:
                    res = self._execute_one()
        except exceptions.SENinjaError:
            res = None
        except Exception as e:
            import os
            _, _, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            sys.stderr.write("Unknown exception in SymbolicExecutor.execute_one():\n")
            sys.stderr.write(" ".join(map(str, ["\t", repr(e), fname, exc_tb.tb_lineno, "\n"])))
            self.put_in_errored(self.state, "Unknown error")
            self.state = None

            res = None

        if res is None:
            if not self.fringe.is_empty():
                self.select_from_deferred()

        return res
