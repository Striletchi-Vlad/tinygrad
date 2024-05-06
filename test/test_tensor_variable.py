import unittest
import numpy as np
from tinygrad import Tensor, Variable

class TestTensorVariable(unittest.TestCase):
  def test_add_tvar(self):
    vv = Variable("a", 0, 10)
    vv.bind(1)
    ret = (Tensor(vv) + 3).item()
    assert ret == 4

  def test_inner_tvar_node(self):
    vv = Variable("w", 0, 10)
    vv.bind(2)
    ret = Tensor.from_node(vv * 4).item()
    assert ret == 8

  def test_inner_tvar_mul(self):
    vv = Variable("w", 0, 10)
    vv.bind(2)
    assert (Tensor(3) * vv).item() == 6

  def test_inner_tvar_mul_node(self):
    vv = Variable("w", 0, 10)
    vv.bind(2)
    assert (Tensor(3) * (vv * 4)).item() == 24

  def test_symbolic_mean(self):
    vv = Variable("a", 1, 10)
    vv.bind(2)
    t = Tensor.ones(2, 2).contiguous().reshape(2, vv)
    ret = t.mean().item()
    assert ret == 1

  @unittest.skip("symbolic var isn't supported")
  def test_symbolic_var(self):
    vv = Variable("a", 1, 10)
    vv.bind(2)
    t = Tensor.ones(2, 2).contiguous().reshape(2, vv)
    ret = t.var().item()
    assert ret == 0

  def test_symbolic_mean_2d(self):
    vv = Variable("a", 1, 10)
    vv.bind(2)
    vv2 = Variable("b", 1, 10)
    vv2.bind(2)
    t = Tensor.ones(2, 2).contiguous().reshape(vv2, vv)
    ret = t.mean().item()
    assert ret == 1

  def test_symbolic_mean_2d_axis_1(self):
    vv = Variable("a", 1, 10)
    vv.bind(2)
    vv2 = Variable("b", 1, 10)
    vv2.bind(2)
    t = Tensor.ones(2, 2).contiguous().reshape(vv2, vv)
    ret = t.mean(axis=1).reshape(2, 1).numpy()
    assert np.all(ret == 1)

  def test_symbolic_arange(self):
    vv = Variable("a", 1, 10)
    vv.bind(2)
    ret = Tensor.arange(0, vv)
    ret.realize()

if __name__ == '__main__':
  unittest.main()
