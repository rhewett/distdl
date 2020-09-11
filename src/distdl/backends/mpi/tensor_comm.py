import numpy as np
from mpi4py import MPI

from distdl.utilities.dtype import intID_to_torch_dtype_dict
from distdl.utilities.dtype import torch_to_intID_dtype_dict
from distdl.utilities.torch import TensorStructure


# Holy cow this is a touchy function, be very careful if modifying it...
def compute_output_tensor_structure(tensor, P_send, P_recv):

    if not P_send.active and not P_recv.active:
        return None, None, None

    requests = []

    if P_send.active:
        # We have the values, set up copies of them to work with
        rg_int_send = np.array([-1], dtype=np.int)
        send_tensor_dim = np.array([len(tensor.shape)], dtype=np.int)
        send_tensor_shape = np.array(tensor.shape, dtype=np.int)

        # Need to send non-Python types, so convert the boolean temporarily
        rg_int_send[0] = 1 if tensor.requires_grad else 0
        req = P_send.comm.Iallreduce(MPI.IN_PLACE, rg_int_send, op=MPI.MAX)
        requests.append(req)

        # Sending processes know the shape, so they can send a copy of the
        # data.  We will ignore this variable later.
        req = P_send.comm.Iallreduce(MPI.IN_PLACE, send_tensor_dim, op=MPI.MAX)
        requests.append(req)

        # Similarly, sending processes know the tensor shape, so they can send
        # a copy of it, but we will not use that copy for our actual return
        # value.
        req = P_send.comm.Iallreduce(MPI.IN_PLACE, send_tensor_shape, op=MPI.MAX)
        requests.append(req)

    # If the process is a receiving process, but doesn't already know the data
    # because it is the _same_ sending process, then we receive the results.
    # If it is a receiving process that sent data to a different set of
    # processes, we still have to complete the receive, even though later we
    # will not use that data.
    if (P_send != P_recv) and P_recv.active:
        # We have the values, set up copies of them to work with
        rg_int_recv = np.array([-1], dtype=np.int)
        recv_tensor_dim = np.array([-1], dtype=np.int)

        # Everyone needs to receive this, but we don't need it for future
        # communication in this function so we can defer receiving the data.
        req = P_recv.comm.Iallreduce(MPI.IN_PLACE, rg_int_recv, op=MPI.MAX)
        requests.append(req)

        # We need this value for the next communication, so we have to wait
        # for it to complete before moving on.
        req = P_recv.comm.Iallreduce(MPI.IN_PLACE, recv_tensor_dim, op=MPI.MAX)
        req.Wait()

        recv_tensor_shape = np.zeros(recv_tensor_dim, dtype=np.int)
        recv_tensor_shape[:] = -1
        req = P_recv.comm.Iallreduce(MPI.IN_PLACE, recv_tensor_shape, op=MPI.MAX)
        requests.append(req)

    # Make sure all requests, including the final recv all reduce complete
    # before receiving processes can actually copy the data out.
    MPI.Request.Waitall(requests)

    # Wait until the communication is complete to set these values.  Only
    # receiving ranks that do not have the data originally should enter here.
    if P_recv.active and (P_send != P_recv):
        tensor_requires_grad = bool(rg_int_recv[0] == 1)
        tensor_dim = recv_tensor_dim[0]
        tensor_shape = recv_tensor_shape
    elif P_send == P_recv:
        tensor_requires_grad = tensor.requires_grad
        tensor_dim = len(tensor.shape)
        tensor_shape = np.array(tensor.shape, dtype=np.int)
    else:
        tensor_requires_grad = None
        tensor_dim = None
        tensor_shape = None

    # Finally, everyone should have valid data.  Any sending rank created it
    # from input data.  Any receving _only_ rank used what it was given.
    return tensor_requires_grad, tensor_dim, tensor_shape

def assemble_global_tensor_structure(local_tensor_structure, P_in, P_out=None):

    global_tensor_structure = TensorStructure()
    global_tensor_shape = None
    intID_dtype = None
    requires_grad_int = None

    if P_in.active:

        # Assemble the global shape
        global_tensor_shape = np.zeros(P_in.dim, dtype=np.int)
        for i in range(P_in.dim):

            keep = [False] * P_in.dim
            keep[i] = True

            P_sub = P_in.create_cartesian_subtopology_partition(keep)

            v0 = np.atleast_1d(int(local_tensor_structure.shape[i]))
            v1 = np.zeros(1, dtype=np.int)
            P_sub.comm.Allreduce(v0, v1, op=MPI.SUM)
            global_tensor_shape[i] = v1[0]

        # Get a communicable integer representing the dtype
        intID_dtype = torch_to_intID_dtype_dict[local_tensor_structure.dtype]
        intID_dtype = np.array([intID_dtype], dtype=np.int)

        requires_grad_int = np.array([-1], dtype=np.int)
        requires_grad_int[0] = 1 if local_tensor_structure.requires_grad else 0

        global_tensor_structure.shape = global_tensor_shape
        global_tensor_structure.dtype = local_tensor_structure.dtype
        global_tensor_structure.requires_grad = local_tensor_structure.requires_grad

    if P_out is not None and P_out.active:
        # Share the shape
        global_tensor_structure.shape = P_out.broadcast_data(global_tensor_shape, P_data=P_in)

        # Share the dtype
        intID_dtype = P_out.broadcast_data(intID_dtype, P_data=P_in)
        global_tensor_structure.dtype = intID_to_torch_dtype_dict[intID_dtype[0]]

        # Share the requires_grad status
        requires_grad_int = P_out.broadcast_data(requires_grad_int, P_data=P_in)
        global_tensor_structure.requires_grad = bool(requires_grad_int[0])

    return global_tensor_structure
