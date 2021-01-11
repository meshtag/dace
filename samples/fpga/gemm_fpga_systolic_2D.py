# Copyright 2019-2020 ETH Zurich and the DaCe authors. All rights reserved.
# Computes C = AB + C
# This sample build on matrix_multiplication_systolic by adding
# vectorization. The systolic arrays used data type depends on the used vectorization width
# (e.g., float16 for vec_width = 16)

import argparse
import dace
import numpy as np
import pdb
import select
import sys

N = dace.symbol("N")
K = dace.symbol("K")
M = dace.symbol("M")
T = dace.symbol("T") #Tile size along M
# Systolic array of size P_R x P_C
P_R = dace.symbol("P_R")
P_C = dace.symbol("P_C")


def make_copy_to_fpga_state(sdfg, vec_width = 1):

    ###########################################################################
    # Copy data to FPGA, from plain to vectorized data type if needed

    state = sdfg.add_state("copy_to_device")
    vec_type = dace.vector(dace.float32, vec_width)

    #host data has plain data types
    sdfg.add_array("A", [N, K], dtype=dace.float32)
    sdfg.add_array("B", [K,  M/vec_width], dtype=vec_type)
    sdfg.add_array("C", [N,  M/vec_width], dtype=vec_type)
    A_host = state.add_read("A")
    B_host = state.add_read("B")
    C_host = state.add_read("C")

    # On the device, vector B and C will be vectorized along rows.
    # Matrix A has plain data type

    sdfg.add_array("A_device", [N, K],
                   dtype=dace.float32,
                   transient=True,
                   storage=dace.dtypes.StorageType.FPGA_Global)
    sdfg.add_array("B_device", [K, M/vec_width],
                   dtype=vec_type,
                   transient=True,
                   storage=dace.dtypes.StorageType.FPGA_Global)
    sdfg.add_array("C_device", [N, M/vec_width],
                   dtype=vec_type,
                   transient=True,
                   storage=dace.dtypes.StorageType.FPGA_Global)
    A_device = state.add_write("A_device")
    B_device = state.add_write("B_device")
    C_device = state.add_write("C_device")

    state.add_memlet_path(A_host,
                          A_device,
                          memlet=dace.Memlet("A_device[0:N, 0:K]"))
    state.add_memlet_path(B_host,
                          B_device,
                          memlet=dace.Memlet("B_device[0:K, 0:M/{}]".format(vec_width)))
    state.add_memlet_path(C_host,
                          C_device,
                          memlet=dace.Memlet("C_device[0:N, 0:M/{}]".format(vec_width)))

    return state


def make_copy_to_host_state(sdfg, vec_width=1):

    ###########################################################################
    # Copy data to FPGA

    state = sdfg.add_state("copy_to_host")

    C_device = state.add_read("C_device")
    C_host = state.add_write("C")

    state.add_memlet_path(C_device, C_host, memlet=dace.Memlet("C[0:N, 0:M/{}]".format(vec_width)))

    return state


def make_read_A(state):

    # The A reader, will read a portion of size P_R of a column of A
    # and will send each element to a different row of the systolic array

    # A is stored with plain data type
    entry, exit = state.add_map("read_A", {
        "n0": "0:N/P_R",
        "tm": "0:M/T", # must be repeated according to the tile size
        "k": "0:K",
        "n1": "0:P_R"
    },
                                schedule=dace.ScheduleType.FPGA_Device)

    mem = state.add_read("A_device")
    pipe = state.add_write("A_pipe")
    tasklet = state.add_tasklet("read_A", {"from_memory"}, {"to_kernel"},
                                "to_kernel = from_memory")

    state.add_memlet_path(mem,
                          entry,
                          tasklet,
                          dst_conn="from_memory",
                          memlet=dace.Memlet("A_device[n0 * P_R + n1, k]"))
    state.add_memlet_path(tasklet,
                          exit,
                          pipe,
                          src_conn="to_kernel",
                          memlet=dace.Memlet("A_pipe[P_R - n1 -1, 0]")) # revert order for convenient draining


def make_read_B(state, sdfg, vec_width=1):

    # TODO: deal with P_C> 1

    # B is vectorized along rows

    #consider also the tiling along M
    #Note: the tile size refers to the non vectorized matrix size
    # To each column of PE, we send a vector

    entry, exit = state.add_map("read_B", {
        "n": "0:N/P_R",
        "tm": "0:M/T",
        "k": "0:K",
        "m": "0:T/({}*P_C)".format(vec_width), # distribution round on each PE column
        "pe_j": "0:P_C"
    },
                                schedule=dace.ScheduleType.FPGA_Device)
    mem = state.add_read("B_device")
    pipe = state.add_write("B_pipe")
    tasklet = state.add_tasklet("read_B", {"from_memory"}, {"to_kernel"},
                                "to_kernel = from_memory")

    #these accesses are vectorized
    state.add_memlet_path(mem,
                          entry,
                          tasklet,
                          dst_conn="from_memory",
                          memlet=dace.Memlet("B_device[k, tm*{}+m*P_C + pe_j ]".format(T/vec_width)))
    state.add_memlet_path(tasklet,
                          exit,
                          pipe,
                          src_conn="to_kernel",
                          memlet=dace.Memlet("B_pipe[0, pe_j]"))



def make_write_C(state, sdfg, vec_width=1):

    # C will read from the draining channels (one for each column of the systolic array)
    # Results are interleaved across PE columns

    # Receives the results and adds it to C

    pipe = state.add_read("C_pipe")
    mem_read = state.add_read("C_device")
    mem = state.add_write("C_device")

    # TODO: here we need a different inner map for P_C
    entry_map, exit_map = state.add_map("write_C", {
        "n0": "0:N/P_R",
        "tm": "0:M/T",
        "n1": "0:P_R",
        "m": "0:T/({}*P_C)".format(vec_width), # results are interleaved across PE
        "pe_j": "0:P_C"
    },
                                        schedule=dace.ScheduleType.FPGA_Device)


    # write in memory by adding itthen we copy that to memory
    tasklet = state.add_tasklet("write_C", {"from_kernel", "prev_c"}, {"to_memory"},
                                "to_memory = from_kernel + prev_c")
    state.add_memlet_path(pipe,
                          entry_map,
                          tasklet,
                          dst_conn="from_kernel",
                          memlet=dace.Memlet("C_pipe[P_R-1, pe_j]"))

    state.add_memlet_path(mem_read,
                          entry_map,
                          tasklet,
                          dst_conn="prev_c",
                          memlet=dace.Memlet("C_device[n0 * P_R + n1, tm*{}+ m*P_C + pe_j]".format(T/vec_width)))

    state.add_memlet_path(tasklet,
                          exit_map,
                          mem,
                          src_conn="to_memory",
                          memlet=dace.Memlet(
                              "C_device[n0 * P_R + n1, tm*{}+ m*P_C + pe_j]".format(T/vec_width)))



def make_compute(sdfg, state, vec_width=1):

    # Each PE computes on each round T_M/vec_width/P_C vectors of the final result

    vec_type = dace.vector(dace.float32, vec_width)
    A_pipe_in = state.add_read("A_pipe")
    A_pipe_out = state.add_write("A_pipe")
    B_pipe_in = state.add_read("B_pipe")
    B_pipe_out = state.add_write("B_pipe")
    C_pipe_in = state.add_read("C_pipe")
    C_pipe_out = state.add_write("C_pipe")

    entry_n0, exit_n0 = state.add_map("n0", {
        "n0": "0:N/P_R",
    },
                                      schedule=dace.ScheduleType.FPGA_Device)
    entry_tm, exit_tm = state.add_map("tm", {"tm": "0:M/T"},
                                    schedule=dace.ScheduleType.FPGA_Device)

    entry_k, exit_k = state.add_map("k", {"k": "0:K"},
                                    schedule=dace.ScheduleType.FPGA_Device)

    # As we are using vectorized data types for B, we have to consider it into these
    # two maps. We have also to consider that are multiple columns of PEs.
    # Given T elements in one tile, one PE produces T/P_C results
    entry_m, exit_m = state.add_map("m", {"m": "0:T/({}*P_C)".format(vec_width)},
                                    schedule=dace.ScheduleType.FPGA_Device)
    entry_c, exit_c = state.add_map("write_C", {
        "n1": "0:P_R",
        "m": "0:T/({}*P_C)".format(vec_width)
    },
                                    schedule=dace.ScheduleType.FPGA_Device)

    # Instantiate buffers
    sdfg.add_scalar("A_reg",
                    dtype=dace.float32,
                    transient=True,
                    storage=dace.dtypes.StorageType.FPGA_Registers)
    A_reg = state.add_write("A_reg")

    # For C result we are going to use vectorized data type
    sdfg.add_array("C_buffer", [T/ vec_width],
                   dtype=vec_type,
                   transient=True,
                   storage=dace.dtypes.StorageType.FPGA_Local)
    C_buffer_in = state.add_read("C_buffer")
    C_buffer_out = state.add_write("C_buffer")

    # every PE: reads input data, and forwards the data
    buffer_a_tasklet = state.add_tasklet(
        "buffer_a", {"a_in"}, {"a_reg", "a_out"}, """\
a_reg = a_in
if p_c < P_C - 1:
    a_out = a_in""")
    state.add_memlet_path(A_pipe_in,
                          entry_n0,
                          entry_tm,
                          entry_k,
                          # entry_a,
                          buffer_a_tasklet,
                          memlet=dace.Memlet("A_pipe[p_r, p_c]", dynamic=False),
                          dst_conn="a_in")
    state.add_memlet_path(buffer_a_tasklet,
                          # exit_a,
                          A_reg,
                          memlet=dace.Memlet("A_reg[0]", dynamic=True),
                          src_conn="a_reg")
    state.add_memlet_path(buffer_a_tasklet,
                          # exit_a,
                          exit_k,
                          exit_tm,
                          exit_n0,
                          A_pipe_out,
                          memlet=dace.Memlet("A_pipe[p_r, p_c + 1]", dynamic=True),
                          src_conn="a_out")
    # Compute and forward B
    compute_tasklet = state.add_tasklet(
        "multiply_add", {"a_in", "b_in", "c_in"}, {"b_out", "c_out"}, """\
c_prev = c_in
if k == 0:
    c_prev = 0
c_out = c_prev + a_in * b_in
if p_r < P_R - 1:
    b_out = b_in""")

    state.add_memlet_path(A_reg,
                          entry_m,
                          compute_tasklet,
                          dst_conn="a_in",
                          memlet=dace.Memlet("A_reg[0]"))
    state.add_memlet_path(B_pipe_in,
                          entry_n0,
                          entry_tm,
                          entry_k,
                          entry_m,
                          compute_tasklet,
                          memlet=dace.Memlet("B_pipe[p_r, p_c]", dynamic=False),
                          dst_conn="b_in")
    state.add_memlet_path(compute_tasklet,
                          exit_m,
                          exit_k,
                          exit_tm,
                          exit_n0,
                          B_pipe_out,
                          memlet=dace.Memlet("B_pipe[p_r + 1, p_c]", dynamic=True),
                          src_conn="b_out")
    state.add_memlet_path(entry_n0, entry_tm, C_buffer_in, memlet=dace.Memlet())
    state.add_memlet_path(C_buffer_in,
                          # entry_tm,
                          entry_k,
                          entry_m,
                          compute_tasklet,
                          dst_conn="c_in",
                          memlet=dace.Memlet("C_buffer[m]"))
    #TODO: not sure if C_buffer goes inside tm loop
    state.add_memlet_path(compute_tasklet,
                          exit_m,
                          exit_k,
                          C_buffer_out,
                          memlet=dace.Memlet("C_buffer[m]"),
                          src_conn="c_out")
    state.add_memlet_path(C_buffer_out, exit_tm, exit_n0, memlet=dace.Memlet())


    write_c_tasklet = state.add_tasklet(
        "write_c", {"buffer_in", "forward_in"}, {"c_out"}, """\
if n1 <= p_r:
    c_out = forward_in if p_r > 0 and n1 > 0 else buffer_in""")
    state.add_memlet_path(C_buffer_out,
                          entry_c,
                          write_c_tasklet,
                          memlet=dace.Memlet("C_buffer[m]", dynamic=True),
                          dst_conn="buffer_in")
    state.add_memlet_path(C_pipe_in,
                          entry_n0,
                          entry_tm,
                          entry_c,
                          write_c_tasklet,
                          memlet=dace.Memlet("C_pipe[p_r-1, p_c]", dynamic=True),
                          dst_conn="forward_in")
    state.add_memlet_path(write_c_tasklet,
                          exit_c,
                          exit_tm,
                          exit_n0,
                          C_pipe_out,
                          memlet=dace.Memlet("C_pipe[p_r, p_c]", dynamic=True),
                          src_conn="c_out")

    # Unroll processing elements
    compute_entry, compute_exit = state.add_map(
        "unroll_compute", {"p_r": "0:P_R", "p_c": "0:P_C"},
        schedule=dace.ScheduleType.FPGA_Device,
        unroll=True)

    # Bring data nodes into scope
    state.add_memlet_path(compute_entry, A_pipe_in, memlet=dace.memlet.Memlet())
    state.add_memlet_path(compute_entry, B_pipe_in, memlet=dace.memlet.Memlet())
    state.add_memlet_path(compute_entry, C_pipe_in, memlet=dace.memlet.Memlet())
    state.add_memlet_path(A_pipe_out, compute_exit, memlet=dace.memlet.Memlet())
    state.add_memlet_path(B_pipe_out, compute_exit, memlet=dace.memlet.Memlet())
    state.add_memlet_path(C_pipe_out, compute_exit, memlet=dace.memlet.Memlet())


def make_fpga_state(sdfg, vec_width=1):
    vec_type = dace.vector(dace.float32, vec_width)

    state = sdfg.add_state("gemm")

    sdfg.add_stream("A_pipe",
                    dace.float32,
                    transient=True,
                    shape=(P_R, P_C+1),
                    storage=dace.dtypes.StorageType.FPGA_Local,
                    buffer_size="P_C")
    sdfg.add_stream("B_pipe",
                    vec_type,
                    transient=True,
                    shape=(P_R , P_C),
                    buffer_size="P_R",
                    storage=dace.dtypes.StorageType.FPGA_Local)
    sdfg.add_stream("C_pipe",
                    vec_type,
                    transient=True,
                    shape=(P_R, P_C),
                    buffer_size="P_R",
                    storage=dace.dtypes.StorageType.FPGA_Local)

    make_read_A(state)
    make_read_B(state, sdfg, vec_width)
    make_compute(sdfg, state, vec_width)
    make_write_C(state, sdfg, vec_width)

    return state


def make_sdfg(specialized, vec_width):

    if specialized:
        sdfg = dace.SDFG("gemm_fpga_systolic_2D_{}x{}_w{}_{}x{}x{}".format(
            P_R.get(), P_C.get(), vec_width,N.get(), K.get(), M.get()))
    else:
        sdfg = dace.SDFG("gemm_fpga_systolic_2D_{}x{}_w{}_NxKx{}".format(
            P_R.get(), P_C.get(), vec_width, M.get()))

    pre_state = make_copy_to_fpga_state(sdfg, vec_width)
    compute_state = make_fpga_state(sdfg, vec_width)
    post_state = make_copy_to_host_state(sdfg, vec_width)

    sdfg.add_edge(pre_state, compute_state, dace.sdfg.InterstateEdge())
    sdfg.add_edge(compute_state, post_state, dace.sdfg.InterstateEdge())
    sdfg.save('/tmp/out.sdfg')
    # sdfg.view()
    return sdfg


if __name__ == "__main__":
    print("==== Program start ====")

    parser = argparse.ArgumentParser()
    parser.add_argument("N", type=int)
    parser.add_argument("M", type=int)
    parser.add_argument("K", type=int)
    parser.add_argument("P_R", type=int, help="Height of the systolic array")
    parser.add_argument("P_C", type=int, help="Width of the systolic array")
    parser.add_argument("W", type=int, help="Vectorization width")
    parser.add_argument("T", type=int, help="Tile size")
    parser.add_argument("-specialize",
                        default=False,
                        action="store_true",
                        help="Fix all loop bounds at compile time/in hardware")
    args = vars(parser.parse_args())
    vec_width = args["W"]
    if not args["specialize"]:
        P_R.set(args["P_R"])
        P_C.set(args["P_C"])
        M.set(args["M"])
        # M must always be specialized, as it's used for the static buffer size
        sdfg = make_sdfg(False, vec_width)
        T.set(args["T"])
        sdfg.specialize(dict(P_R=P_R, P_C=P_C, M=M, T=T))
        N.set(args["N"])
        K.set(args["K"])
    else:
        P_R.set(args["P_R"])
        P_C.set(args["P_C"])
        M.set(args["M"])
        N.set(args["N"])
        K.set(args["K"])
        T.set(args["T"])
        sdfg = make_sdfg(True, vec_width)
        sdfg.specialize(dict(P_R=P_R, P_C=P_C, M=M, N=N, K=K, T=T))

    print("Matrix multiplication {}x{}x{} with {}x{} PEs ({}specialized)".format(
        M.get(), N.get(), K.get(), P_R.get(),P_C.get(),
        "" if args["specialize"] else "not "))

    # Initialize arrays: Randomize A and B, zero C
    A = np.ndarray([N.get(), K.get()], dtype=dace.float32.type)
    B = np.ndarray([K.get(), M.get()], dtype=dace.float32.type)
    C = np.ndarray([N.get(), M.get()], dtype=dace.float32.type)
    A[:] = np.random.rand(N.get(), K.get()).astype(dace.float32.type)
    B[:] = np.random.rand(K.get(), M.get()).astype(dace.float32.type)
    C[:] = np.random.rand(N.get(), M.get()).astype(dace.float32.type)

    C_regression = np.ndarray([N.get(), M.get()], dtype=np.float32)

    # compute ground truth
    C_regression = A@B +C


    if args["specialize"]:
        sdfg(A=A, B=B, C=C)
    else:
        sdfg(A=A, B=B, C=C, N=N, K=K)
    diff = np.linalg.norm(C_regression - C) / float(M.get() * K.get())
    if diff > 1e-6:
        raise ValueError(f"Verification failed, difference: {diff}")
    else:
        print("Results successfully verified.")
