#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""This is the lib for gradient checker unittest."""

from __future__ import print_function

import unittest
import six
import collections
import numpy as np
from itertools import product

import paddle.fluid as fluid
import paddle.fluid.core as core
from paddle.fluid.executor import Executor
from paddle.fluid.backward import _append_grad_suffix_, _as_list


def _product(t):
    if isinstance(t, int):
        return t
    else:
        return np.product(t)


def dtype_to_np_dtype(dtype):
    if dtype == core.VarDesc.VarType.FP32:
        return np.float32
    elif dtype == core.VarDesc.VarType.FP64:
        return np.float64
    elif dtype == core.VarDesc.VarType.FP16:
        return np.float16
    else:
        raise ValueError("Not supported data type " + str(dtype))


def _get_item(t, i, np_dtype):
    if np_dtype == np.float16:
        np_t = np.array(t).astype(np.float16)
        np_t = np_t.flatten()
        return np_t[i]
    elif np_dtype == np.float32:
        return t._get_float_element(i)
    elif np_dtype == np.float64:
        return t._get_double_element(i)
    else:
        raise ValueError("Not supported data type " + str(np_dtype))


def _set_item(t, i, e, np_dtype):
    if np_dtype == np.float16:
        np_t = np.array(t).astype(np.float16)
        shape = np_t.shape
        np_t = np_t.flatten()
        np_t[i] = e
        np_t = np_t.reshape(shape)
        t.set(np_t, place)
    elif np_dtype == np.float32:
        t._set_float_element(i, e)
    elif np_dtype == np.float64:
        t._set_double_element(i, e)
    else:
        raise ValueError("Not supported data type " + str(np_dtype))


def set_var_in_scope(scope, place, name, value, recursive_seq_len=None):
    t = scope.var(name).get_tensor()
    t.set(value, place)
    if recursive_seq_len:
        t.set_recursive_sequence_lengths(recursive_seq_len)
    return t


def var_to_np_array_in_scope(scope, place, name):
    return np.array(scope.var(name).get_tensor())


def make_jacobian(x, y_size, np_dtype):
    print(" === in make_jacobian func ===, x:", x)
    print(" === in make_jacobian func ===, y.size:", y_size)
    if isinstance(x, fluid.framework.Variable):
        return np.zeros((_product(x.shape), y_size), dtype=np_dtype)
    elif isinstance(x, collections.Sequence):
        jacobians = list(
            filter(lambda t: t is not None, (make_jacobian(
                item, y_size, np_dtype) for item in x)))
        return jacobians
    else:
        None


def _compute_numerical_jacobian(program, x, y, place, scope, delta):
    """Computes the numeric Jacobian for dy/dx.

    Computes the numeric Jacobian by slightly perturbing the inputs and
    measuring the differences on the output.

    Args:
        program (Program): the network program.
        x (Variable): the input variables.
        y (list[Variable]): the output variables.
        place (fluid.CPUPlace or fluid.CUDAPlace): the device.
        scope (Scope): the scope used to run program.
        delta: the amount of perturbation we give to the input

    Returns:
        A list of 2-D numpy array, the list length is len(y).
        Each 2-D numpy array represents the Jacobian for dy_i/dx.
        It has "x_size" rows and "y_size" columns
        where "x_size" is the number of elements in x and
        "y_size" is the number of elements in each y_i.
    """
    print("======== in _numerical_ begging, x:", _as_list(x))
    print("======== in _numerical_ begging, y:", _as_list(y))
    if not isinstance(x, fluid.framework.Variable):
        raise TypeError('x is not Variable')

    # To compute the jacobian, treat x and y as one-dimensional vectors.
    y = _as_list(y)
    exe = fluid.Executor(place)

    def run():
        y_res = exe.run(program, scope=scope, fetch_list=y)
        print("===== y_res:", y_res)
        print("===== y_res length:", len(y_res))
        return [yi.flatten() for yi in y_res]

    x_name = x.name
    x_shape = x.shape
    x_size = _product(x_shape)
    x_t = scope.find_var(x_name).get_tensor()

    np_type = dtype_to_np_dtype(x.dtype)
    jacobian = [make_jacobian(x, _product(yi.shape), np_type) for yi in y]

    for i in six.moves.xrange(x_size):
        orig = _get_item(x_t, i, np_type)
        x_pos = orig + delta
        _set_item(x_t, i, x_pos, np_type)
        y_pos = run()

        x_neg = orig - delta
        _set_item(x_t, i, x_neg, np_type)
        y_neg = run()

        _set_item(x_t, i, orig, np_type)

        for j in six.moves.xrange(len(y)):
            # print("=========== set_item", y_pos[j])
            # print("=========== set_item", y_neg[j])
            # print("=========== set_item", y_pos[j] - y_neg[j])
            # print("=========== set_item", (y_pos[j] - y_neg[j]) / delta)
            # print("=========== set_item", (y_pos[j] - y_neg[j]) / delta / 2)

            jacobian[j][i, :] = (y_pos[j] - y_neg[j]) / delta / 2.
            print("====== six.move result:", jacobian[j][i, :])
    print("======= _compute_numerical_jacobian   ---- return jacobian =====:",
          jacobian)
    return jacobian


def _compute_analytical_jacobian(program, x, y, place, scope):
    """Computes the analytical Jacobian for dy/dx.

    Args:
        program (Program): a Program with forward pass.
        x (Variable|list[Variable]): a variable or list of variable
        y (Variable): the target variable.
        place (fluid.CPUPlace or fluid.CUDAPlace): the device.
        scope (Scope): the scope used to run program.

    Returns:
        A list of 2-D numpy array. The list length is len(x).
        Each 2-D numpy array represents the Jacobian for dy/dx_i.
        It has "xi_size" rows and "dy_size" columns
        where "x_size" is the number of elements in x_i and
        "dy_size" is the number of elements in y.
    """
    if program is None:
        program = fluid.default_main_program()
    # print("======== in _analytical_ begging, x:", _as_list(x))
    # print("======== in _analytical_ begging, y:", _as_list(y))
    if not isinstance(y, fluid.framework.Variable):
        raise TypeError('y is not Variable')

    dy_name = _append_grad_suffix_(y.name)

    np_type = dtype_to_np_dtype(y.dtype)
    # create dy Variable in Program
    dy = program.global_block().create_var(
        name=dy_name, shape=y.shape, dtype=np_type, persistable=True)
    # append backward
    print("======= in _compute_analytical_jacobian func: -> y:", y)
    print("======= in _compute_analytical_jacobian func: -> x:", x)
    print("======= in _compute_analytical_jacobian func: -> dy:", dy)
    # print("======= in _compute_analytical_jacobian func: -> type(y):", type(y))
    # print("======= in _compute_analytical_jacobian func: -> type(x):", type(x))
    # print("======= in _compute_analytical_jacobian func: -> type(dy):", type(dy))
    # print("========== in analytical_jacobian begging, y.shape:", y.shape)
    # print(" in _compute_analytical_jacobian , len(program.global_block().ops) ==,", len(program.global_block().ops))
    # print(" in _compute_analytical_jacobian , program.global_block().ops ======,", program.global_block().ops)
    dx = fluid.gradients(y, x, dy)
    print("============= in _compute_analytical_jacobian : ==> dx", dx)
    # print(" in _compute_analytical_jacobian , len(program.global_block().ops) ==,", len(program.global_block().ops))
    # print(" in _compute_analytical_jacobian , program.global_block().ops ======,", program.global_block().ops)
    # init dy tensor in scope
    value = np.zeros(y.shape, dtype=np_type)
    print("======= value,", value)
    dy_t = set_var_in_scope(scope, place, dy_name, value)
    print("======= dy_t,", dy_t)
    exe = fluid.Executor(place)
    print("========== in analytical_jacobian, y.shape:", y.shape)
    y_size = _product(y.shape)
    print("========== in analytical_jacobian, y_size:", y_size)
    x = _as_list(x)
    jacobian = make_jacobian(x, y_size, np_type)
    print(" == == == after make_jacobian func == == == ", jacobian)

    check = [(i, dxi) for i, dxi in enumerate(dx) if dxi is not None]
    print("=========== checking  ===", check)

    # filter None in dx for DX/DY may be None in kernel
    # only fetch not None dx in exe.run
    filted = [(i, dxi) for i, dxi in enumerate(dx) if dxi is not None]
    filted_idx, filted_dx = zip(*filted)
    print("=== filted_idx: ", filted_idx)
    print("=== filted_dx: ", filted_dx)

    for i in six.moves.xrange(y_size):
        _set_item(dy_t, i, 1, np_type)  # set dy_i => 1

        dx_res = exe.run(program, scope=scope, fetch_list=filted_dx)
        print("=========== dx_res,", dx_res)

        for j in six.moves.xrange(len(filted_dx)):
            dx_idx = filted_idx[j]
            if dx_res[j] is not None:
                jacobian[dx_idx][:, i] = dx_res[j].flatten()
            else:
                jacobian[dx_idx][:, i] = np.zeros(
                    dx[dx_idx].shape, dtype=np_type).flatten()

        _set_item(dy_t, i, 0, np_type)
    print("======= _compute_analytical_jacobian   ---- return jacobian =====:",
          jacobian)
    return jacobian


def grad_check(
        x,  # in 
        y,  # out
        x_init=None,
        place=None,
        program=None,
        eps=1e-6,
        atol=1e-5,
        rtol=1e-3,
        raise_exception=True):
    print("============== in grad_check beginning ===============")
    """
    Check numerical and analytical gradients for dy/dx.
    Each Jacobian gradients is a 2-D array with shape [xi_size, yi_size].

    Args:
        x (Variable|list[Variable]): input variables to the program.
        y (Variable|list[Variable]): output variables to the program.
        x_init (numpy.array|list[numpy.array]|None): the init value for input x.
        place (fluid.CPUPlace or fluid.CUDAPlace): the device.
        program (Program|None): a Program with forward pass.
            If None, use fluid.default_main_program().
        eps (float): perturbation for finite differences.
        atol (float): absolute tolerance.
        rtol (float): relative tolerance.
        raise_exception (bool): whether to raise an exception if
            the check fails. Default is True.
    Returns:
        True if all differences satisfy numpy.allclose condition.
    """

    def fail_test(msg):
        if raise_exception:
            raise RuntimeError(msg)
        return False

    # check input arguments
    x = _as_list(x)
    y = _as_list(y)
    print("====== in grad_check, y counts", len(y))
    print("====== in grad_check, y:", y)

    for v in x:
        v.stop_gradient = False
        v.persistable = True
    if place is None:
        place = fluid.CPUPlace()
    if program is None:
        program = fluid.default_main_program()

    # init variable in strtup program
    scope = fluid.executor.global_scope()
    exe = fluid.Executor(place)
    exe.run(fluid.default_startup_program())

    x_init = _as_list(x_init)
    # init inputs if x_init is not None
    if x_init:
        if len(x_init) != len(x):
            raise ValueError('len(x_init) (=%d) is not the same'
                             ' as len(x) (= %d)' % (len(x_init), len(x)))
        # init variable in main program
        for var, arr in zip(x, x_init):
            assert var.shape == arr.shape
        feeds = {k.name: v for k, v in zip(x, x_init)}
        exe.run(program, feed=feeds, scope=scope)

    print(
        "=========== in grad check, len(program.global_block().ops) ==========,",
        len(program.global_block().ops))
    print("=========== in grad check, program.global_block().ops ==========,",
          program.global_block().ops)

    # [x_idx, y_idx]
    numerical = [
        _compute_numerical_jacobian(program, xi, y, place, scope, eps)
        for xi in x
    ]

    # [y_idx, x_idx]
    analytical = []
    print("============== in grad check, y:", y)
    for yi in y:
        print("============== in grad check, yi:", yi)
        print("============== in grad check, yi.shape:", yi.shape)
        print("============== in grad check, yi.shape' type:", type(yi.shape))
        print("============== in grad check, _product(yi.shape):",
              _product(yi.shape))
        prog = program.clone()
        clone_x = []
        clone_y = None
        for b in prog.blocks:
            if b.has_var(yi.name):
                clone_y = b.var(yi.name)
                break
        for xi in x:
            for b in prog.blocks:
                if b.has_var(xi.name):
                    clone_x.append(b.var(xi.name))
                    break

        analytical.append(
            _compute_analytical_jacobian(prog, clone_x, clone_y, place, scope))
        print("============== analytical:", analytical)
        # if _product(yi.shape) == 0:
        #     print(" ==== === == ==  Invalid y  =========== == ")
        # else:
        #     prog = program.clone()
        #     clone_x = []
        #     clone_y = None
        #     for b in prog.blocks:
        #         if b.has_var(yi.name):
        #             clone_y = b.var(yi.name)
        #             break
        #     for xi in x:
    #     for b in prog.blocks:
    #         if b.has_var(xi.name):
    #             clone_x.append(b.var(xi.name))
    #             break

    # analytical.append(
    #     _compute_analytical_jacobian(prog, clone_x, clone_y, place, scope))
    # print("============== analytical:", analytical)

    for i, (x_idx,
            y_idx) in enumerate(product(* [range(len(x)), range(len(y))])):
        a = analytical[y_idx][x_idx]
        n = numerical[x_idx][y_idx]
        if not np.allclose(a, n, rtol, atol):
            msg = 'Jacobian mismatch for output %s ' \
                  'with respect to input %s on %s,\n' \
                  'numerical:%s\nanalytical:%s\n' \
                  % (y[y_idx].name, x[x_idx].name, str(place), n, a)
            return fail_test(msg)
    print("============== in grad_check end ===============")
    print(
        "=========== in grad check, len(program.global_block().ops) ==========,",
        len(program.global_block().ops))
    print("=========== in grad check, program.global_block().ops ==========,",
          program.global_block().ops)
    return True


def double_grad_check(x,
                      y,
                      x_init=None,
                      y_grads=None,
                      place=None,
                      program=None,
                      eps=1e-6,
                      atol=1e-5,
                      rtol=1e-3,
                      raise_exception=True):
    print("============== in double_grad_check beginning ===============")
    print("== in double_grad_check begging : x.len:", len(_as_list(x)))
    print("== in double_grad_check begging : y.len:", len(_as_list(y)))
    print("== in double_grad_check begging : x_init.len:",
          len(_as_list(x_init)))
    """
    Check gradients of gradients. This function will append backward to the
    program before second order gradient check.

    Args:
        x (Variable|list[Variable]): input variables to the program.
        y (Variable|list[Variable]): output variables to the program.
        x_init (numpy.array|list[numpy.array]|None): the init value for input x.
        y_grads (numpy.array|list[numpy.array]|None): the gradients with respect to y.
        place (fluid.CPUPlace or fluid.CUDAPlace): the device.
        program (Program|None): a Program with forward pass.
            If None, use fluid.default_main_program().
        eps (float): perturbation for finite differences.
        atol (float): absolute tolerance.
        rtol (float): relative tolerance.
        raise_exception (bool): whether to raise an exception if
            the check fails. Default is True.
    Returns:
        True if all differences satisfy numpy.allclose condition.
    """
    # check input arguments
    x = _as_list(x)
    for v in x:
        v.stop_gradient = False
        v.persistable = True
    y = _as_list(y)
    print("====== in double grad_check, y counts", len(y))
    print("====== in double grad_check, y:", y)

    if program is None:
        program = fluid.default_main_program()

    if y_grads is None:
        scope = fluid.executor.global_scope()
        y_grads = []
        y_grads_init = []
        for yi in y:
            dyi_name = _append_grad_suffix_(yi.name)
            print("========= double_grad_check:=======", dyi_name)
            np_type = dtype_to_np_dtype(yi.dtype)
            dy = program.global_block().create_var(
                name=dyi_name, shape=yi.shape, dtype=np_type, persistable=True)
            dy.stop_gradient = False
            v = np.ones(yi.shape).astype(np_type)
            # v = np.random.random(size=yi.shape).astype(np_type)
            set_var_in_scope(scope, place, dyi_name, v)
            y_grads.append(dy)
            y_grads_init.append(v)
    else:
        y_grads = _as_list(y_grads)
        y_grads_init = [
            var_to_np_array_in_scope(scope, place, v.name) for v in y_grads
        ]

    # append first order grads
    target_grads = fluid.gradients(y, x, y_grads)
    print("============ in double_grad_check  == target_grads:", target_grads)
    # 将 y 的梯度反向传播到输入 Tensor x, y_grads 如果设置为 None，则以 1 初始化所有梯度 Tensor。

    # target_grads_grads = fluid.gradients(target_grads, x)
    # print("==========  target_grads_grads  =========== ", target_grads_grads)
    # target_grads_grads_grads = fluid.gradients(target_grads_grads, x)
    # print("==========  target_grads_grads_grads  =========== ", target_grads_grads_grads)
    # y_grads are the input of first-order backward,
    # so, they are also the input of second-order backward.
    x += y_grads
    x_init = _as_list(x_init)
    x_init += y_grads_init
    print("== in double_grad_check: y.len:", len(_as_list(y)))
    print("== in double_grad_check: x.len:", len(_as_list(x)))
    print("== in double_grad_check: target.len:", len(_as_list(target_grads)))
    print("== in double_grad_check: x_init.len :", len(_as_list(x_init)))

    print(
        "=========== in double grad check, len(program.global_block().ops) ==========,",
        len(program.global_block().ops))
    print(
        "=========== in double grad check, program.global_block().ops ==========,",
        program.global_block().ops)

    grad_check(x, target_grads, x_init, place, program, eps, atol, rtol)
    print("============== in double_grad_check end ===============")


# grad_check(x, y,...)  y 对 x 求导
# grad_check(x, grads, ...)  grads 对 x 求导
# grad_check(x, grad_grads, ...) grad_grads 对 x 求导 
def trible_grad_check(x,
                      y,
                      x_init=None,
                      y_grads=None,
                      place=None,
                      program=None,
                      eps=1e-6,
                      atol=1e-5,
                      rtol=1e-3,
                      raise_exception=True):

    print("============== in trible_grad_check begging ===============")

    # check input arguments
    x = _as_list(x)
    for v in x:
        v.stop_gradient = False
        v.persistable = True
    y = _as_list(y)

    if program is None:
        program = fluid.default_main_program()

    if y_grads is None:
        scope = fluid.executor.global_scope()
        y_grads = []
        y_grads_init = []
        for yi in y:
            dyi_name = _append_grad_suffix_(yi.name)
            print("========= trible_grad_check:=======", dyi_name)
            np_type = dtype_to_np_dtype(yi.dtype)
            dy = program.global_block().create_var(
                name=dyi_name, shape=yi.shape, dtype=np_type, persistable=True)
            dy.stop_gradient = False
            v = np.ones(yi.shape).astype(np_type)
            # v = np.random.random(size=yi.shape).astype(np_type)
            set_var_in_scope(scope, place, dyi_name, v)
            y_grads.append(dy)
            y_grads_init.append(v)
    else:
        y_grads = _as_list(y_grads)
        y_grads_init = [
            var_to_np_array_in_scope(scope, place, v.name) for v in y_grads
        ]
    print("== == == == == y_grads: ", y_grads)
    # append first order grads
    target_grads = fluid.gradients(y, x, y_grads)
    print("============ in trible_grad_check  == target_grads:", target_grads)
    # target_grads_grads = fluid.gradients(target_grads_grads, x, y_grads)

    # y_grads are the input of first-order backward,
    # so, they are also the input of second-order backward.
    x += y_grads
    x_init = _as_list(x_init)
    x_init += y_grads_init
    print("=========== len(program.global_block().ops) ==========,",
          len(program.global_block().ops))
    print("=========== program.global_block().ops ==========,",
          program.global_block().ops)
    double_grad_check(
        x=x,
        y=target_grads,
        x_init=x_init,
        place=place,
        program=program,
        eps=eps,
        atol=atol,
        rtol=rtol)
    print("============== in trible_grad_check end ===============")


def trible_grad_check_2(x,
                        y,
                        x_init=None,
                        y_grads=None,
                        place=None,
                        program=None,
                        eps=1e-6,
                        atol=1e-5,
                        rtol=1e-3,
                        raise_exception=True):

    print("============== in trible_grad_check_2 begging ===============")
    # check input arguments
    x = _as_list(x)
    for v in x:
        v.stop_gradient = False
        v.persistable = True
    y = _as_list(y)

    if program is None:
        program = fluid.default_main_program()

    # if y_grads is None:
    #     scope = fluid.executor.global_scope()
    #     y_grads = []
    #     y_grads_init = []
    #     for yi in y:
    #         dyi_name = _append_grad_suffix_(yi.name)
    #         print("========= trible_grad_check_2:=======", dyi_name)
    #         np_type = dtype_to_np_dtype(yi.dtype)
    #         dy = program.global_block().create_var(
    #             name=dyi_name, shape=yi.shape, dtype=np_type, persistable=True)
    #         dy.stop_gradient = False
    #         v = np.random.random(size=yi.shape).astype(np_type)
    #         set_var_in_scope(scope, place, dyi_name, v)
    #         y_grads.append(dy)
    #         y_grads_init.append(v)
    # else:
    #     y_grads = _as_list(y_grads)
    #     y_grads_init = [
    #         var_to_np_array_in_scope(scope, place, v.name) for v in y_grads
    #     ]

    # append first order grads
    target_grads = fluid.gradients(y, x, y_grads)
    print("============ in trible_grad_check_2  == target_grads:", target_grads)

    y_grads_grads = None
    # if y_grads_grads is None:
    #     scope = fluid.executor.global_scope()
    #     y_grads_grads = []
    #     y_grads_grads_init = []
    #     for yi in target_grads:
    #         dyi_name = _append_grad_suffix_(yi.name)
    #         print("========= trible_grad_check_2:=======", dyi_name)
    #         np_type = dtype_to_np_dtype(yi.dtype)
    #         dy = program.global_block().create_var(
    #             name=dyi_name, shape=yi.shape, dtype=np_type, persistable=True)
    #         dy.stop_gradient = False
    #         v = np.random.random(size=yi.shape).astype(np_type)
    #         set_var_in_scope(scope, place, dyi_name, v)
    #         y_grads_grads.append(dy)
    #         y_grads_grads_init.append(v)

    # x += y_grads

    target_grads_grads = fluid.gradients(target_grads, x, y_grads_grads)
    print("============ in trible_grad_check_2  == target_grads:",
          target_grads_grads)
    # y_grads are the input of first-order backward,
    # so, they are also the input of second-order backward.

    x_init = _as_list(x_init)
    # x_init += y_grads_init

    # x += y_grads_grads
    # x_init += y_grads_grads_init

    grad_check(
        x=x,
        y=target_grads_grads,
        x_init=x_init,
        place=place,
        program=program,
        eps=eps,
        atol=atol,
        rtol=rtol)
    print("============== in trible_grad_check_2 end ===============")


def trible_test(x,
                y,
                x_init=None,
                y_grads=None,
                place=None,
                program=None,
                eps=1e-6,
                atol=1e-5,
                rtol=1e-3,
                raise_exception=True):
    x = _as_list(x)
    for v in x:
        v.stop_gradient = False
        v.persistable = True
    y = _as_list(y)
    # print("y:", y)
    # print("x:", x)
    target_grads = fluid.gradients(y, x, y_grads)
    print("======= in grad_test func: target_grads length:", len(target_grads))
    print("======= in grad_test func: target_grads:", target_grads)
    target_grads_grads = fluid.gradients(target_grads, x, y_grads)
    print("======= in grad_test func: -> target_grads_grads:",
          target_grads_grads)
    print("======= in grad_test func: -> x:", x)
    print("======= in grad_test func: -> y_grads:", y_grads)
    print("===== in grad_test func: -> type(target_grads_grads[0]):",
          type(target_grads_grads[0]))
    target_grads_grads_grads = fluid.gradients(target_grads_grads, x, y_grads)

    if place is None:
        place = fluid.CPUPlace()
    if program is None:
        program = fluid.default_main_program()

    # init variable in strtup program
    scope = fluid.executor.global_scope()
    exe = fluid.Executor(place)
    exe.run(fluid.default_startup_program())

    x_init = _as_list(x_init)
    # init inputs if x_init is not None
    if x_init:
        if len(x_init) != len(x):
            raise ValueError('len(x_init) (=%d) is not the same'
                             ' as len(x) (= %d)' % (len(x_init), len(x)))
        # init variable in main program
        for var, arr in zip(x, x_init):
            assert var.shape == arr.shape
        feeds = {k.name: v for k, v in zip(x, x_init)}
        grad0, = exe.run(program,
                         feed=feeds,
                         scope=scope,
                         fetch_list=[zi.name for zi in target_grads])
        grad1, = exe.run(program,
                         feed=feeds,
                         scope=scope,
                         fetch_list=[zi.name for zi in target_grads_grads])
        grad2, = exe.run(
            program,
            feed=feeds,
            scope=scope,
            fetch_list=[zi.name for zi in target_grads_grads_grads])
    print("============================== ", grad0)
    print("============================== ", grad1)
    print("============================== ", grad2)
    # grad_check(x = x, y = target_grads_grads, x_init = x_init, place = place, 
    #                   program = program, eps = eps, atol = atol, rtol = rtol)

    print(" in trible_test , len(program.global_block().ops) ==,",
          len(program.global_block().ops))
    print(" in trible_test , program.global_block().ops ======,",
          program.global_block().ops)
    print("====  target_grads_grads:", target_grads_grads)

    numerical = [
        _compute_numerical_jacobian(program, xi, target_grads_grads, place,
                                    scope, eps) for xi in x
    ]
    print("============ trible_test numerical", numerical)

    # with open("main_prog.txt", "w") as fout:
    #     try:
    #         # compatible with paddle version <= 1.8
    #         from paddle.fluid.transpiler.details import program_to_code
    #         program_to_code(fluid.default_main_program(), fout=fout)
    #     except ImportError:
    #         fout.write(str(fluid.default_main_program()))
