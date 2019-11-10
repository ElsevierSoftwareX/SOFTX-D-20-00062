class Os(object):
    # syscall
    def get_syscall_by_number(self, n):
        raise NotImplementedError
    def get_syscall_n_reg(self):
        raise NotImplementedError
    def get_syscall_parameter(self, k):
        raise NotImplementedError
    def get_out_syscall_reg(self):
        raise NotImplementedError
    # devices
    def open(self, fd: int):
        raise NotImplementedError
    def is_open(self, fd: int):
        raise NotImplementedError
    def close(self, fd: int):
        raise NotImplementedError
    def get_device_by_fd(self, fd: int):
        raise NotImplementedError
    def get_stdin(self):
        raise NotImplementedError
    def get_stdout(self):
        raise NotImplementedError
    # other
    def copy(self):
        raise NotImplementedError
    def merge(self, other, merge_condition):
        raise NotImplementedError
