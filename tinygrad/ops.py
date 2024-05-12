from __future__ import annotations
from typing import Union, Tuple, Any, List, Dict, Callable
import functools, hashlib, math, operator, ctypes
from enum import Enum, auto
from dataclasses import dataclass
from tinygrad.helpers import prod, dedup
from tinygrad.dtype import dtypes, DType, ConstType
from tinygrad.shape.symbolic import Variable, sint
from tinygrad.shape.shapetracker import ShapeTracker

# these are the llops your accelerator must implement, along with toCpu
# the Enum class doesn't work with mypy, this is static. sorry it's ugly
# NOTE: MOD, CMPLT don't have to be implemented on vectors, just scalars
# NOTE: many GPUs don't have DIV, but UnaryOps.RECIP doesn't work for integer division
class UnaryOps(Enum):
  """A -> A (elementwise)"""
  EXP2 = auto(); LOG2 = auto(); CAST = auto(); SIN = auto(); SQRT = auto(); NEG = auto() # noqa: E702
class BinaryOps(Enum):
  """A + A -> A (elementwise)"""
  ADD = auto(); SUB = auto(); MUL = auto(); DIV = auto(); MAX = auto(); MOD = auto(); CMPLT = auto(); CMPEQ = auto(); XOR = auto() # noqa: E702
class TernaryOps(Enum):
  """A + A + A -> A (elementwise)"""
  WHERE = auto(); MULACC = auto() # noqa: E702
class ReduceOps(Enum):
  """A -> B (reduce)"""
  SUM = auto(); MAX = auto() # noqa: E702
class BufferOps(Enum): LOAD = auto(); CONST = auto(); STORE = auto() # noqa: E702
class LoadOps(Enum): EMPTY = auto(); CONST = auto(); COPY = auto(); CONTIGUOUS = auto(); CUSTOM = auto(); ASSIGN = auto(); VIEW = auto() # noqa: E702

Op = Union[UnaryOps, BinaryOps, ReduceOps, LoadOps, TernaryOps, BufferOps]

# do not preserve f(0) = 0
UNSAFE_PAD_OPS = {BinaryOps.DIV, BinaryOps.CMPLT, BinaryOps.CMPEQ, UnaryOps.LOG2, UnaryOps.EXP2}

@dataclass(frozen=True)
class MemBuffer:
  idx: int
  dtype: DType
  st: ShapeTracker

@dataclass(frozen=True)
class ConstBuffer:
  val: ConstType
  dtype: DType
  st: ShapeTracker

@dataclass(frozen=True, eq=False)
class LazyOp:
  op: Op
  src: Tuple[LazyOp, ...] = ()
  arg: Any = None
  def cached_compare(self, x, context):
    if id(self) == id(x): return True
    if self.op != x.op or self.arg != x.arg or len(self.src) != len(x.src): return False
    if (key := (id(self), id(x))) in context: return context[key]
    ret = context[key] = all(a.cached_compare(b, context) for a,b in zip(self.src, x.src))
    return ret
  def __eq__(self, x): return self.cached_compare(x, context={})
  def __repr__(self): return f"LazyOp(op={self.op}, src={self.src}, arg={self.arg})"
  @functools.cached_property
  def dtype(self) -> DType:
    if self.op in BufferOps: return self.arg.dtype
    if self.op is UnaryOps.CAST: return self.arg[0]
    return dtypes.bool if self.op in {BinaryOps.CMPLT, BinaryOps.CMPEQ} else self.src[-1].dtype

  @functools.cached_property
  def key(self) -> bytes:
    return hashlib.sha256(functools.reduce(lambda x,y: x+y, [s.key for s in self.src], str((self.op, self.arg)).encode())).digest()
  @functools.cached_property
  def hash(self): return hash((self.op, self.src, self.arg))
  def __hash__(self): return self.hash
  @functools.cached_property
  def lazyops(self) -> List[LazyOp]: return dedup([self] + [item for x in self.src for item in x.lazyops])
  def vars(self) -> List[Variable]:
    extract_vars = [x.arg.st.vars() for x in self.lazyops if x.op in BufferOps]
    const_vars = [x.arg.val.unbind()[0] for x in self.lazyops if x.op is BufferOps.CONST and isinstance(x.arg.val, Variable)]
    return sorted(set.union(*extract_vars, set(const_vars)), key=lambda x: str(x.expr))

# **************** independent FlopCounter ****************

@dataclass
class FlopCounter:
  shape: Tuple[int, ...]
  flops: sint
  mem: Dict[int, int]
  @property
  def mem_estimate(self): return sum(self.mem.values())
  def consume_flops(self):
    self.flops, ret = 0, self.flops
    return ret

InterpretedFlopCounter: Dict[Op, Callable] = {
  BufferOps.LOAD: lambda arg: FlopCounter(arg.st.shape, 0, {arg.idx: arg.dtype.itemsize * arg.st.real_size()}),
  BufferOps.CONST: lambda arg: FlopCounter(arg.st.shape, 0, {}),
  BufferOps.STORE: lambda self,arg: FlopCounter(arg.st.shape, self.consume_flops(), {**self.mem, arg.idx: arg.dtype.itemsize * arg.st.real_size()}),
  UnaryOps.CAST: lambda self,arg: FlopCounter(self.shape, self.consume_flops(), self.mem),   # cast uses no flops
  **{op:lambda self: FlopCounter(self.shape, self.consume_flops() + prod(self.shape), self.mem) for op in UnaryOps if op is not UnaryOps.CAST},
  **{op:lambda self,y: FlopCounter(self.shape, self.consume_flops() + y.consume_flops() + prod(self.shape), {**self.mem, **y.mem}) for op in BinaryOps},  # noqa: E501
  **{op:lambda self,axis: FlopCounter(tuple(1 if i in axis else s for i,s in enumerate(self.shape)), self.consume_flops() + prod(self.shape), self.mem) for op in ReduceOps},  # noqa: E501
  TernaryOps.WHERE: lambda self,y,z: FlopCounter(self.shape, self.consume_flops() + y.consume_flops() + z.consume_flops() + prod(self.shape), {**self.mem, **y.mem, **z.mem})}  # noqa: E501

@functools.lru_cache(None)
def get_lazyop_info(ast:LazyOp) -> FlopCounter:
  @functools.lru_cache(None) # NOTE: this cache needs to be recreated for new ASTs
  def run_ast(ast): return InterpretedFlopCounter[ast.op](*([run_ast(x) for x in ast.src]+([ast.arg] if ast.arg is not None else [])))
  return run_ast(ast)

# **************** ops in python ****************

def hook_overflow(dv, fxn):
  def wfxn(*args):
    try: return fxn(*args)
    except OverflowError: return dv
  return wfxn

def taylor_sin(x, n=25):
    x %= 2 * math.pi
    res = 0
    for i in range(n):
        res += math.pow(-1, i) * math.pow(x, 2 * i + 1) / math.factorial(2 * i + 1)
    return res

def taylor_log2(x, n=25):
    x -= 1
    res = 0
    for i in range(1, n):
        res += math.pow(-1, i + 1) * math.pow(x, i) / i
    return res * 1.4426950408889634

def taylor_exp2(x, n=25):
    ln2 = 0.6931471805599453
    res = 1 + ln2 * x
    for i in range(2, n):
        res += math.pow(ln2, i) * math.pow(x, i) / math.factorial(i)
    return res

python_alu = {
  UnaryOps.LOG2: lambda x: taylor_log2(x) if x > 0 else -math.inf if x == 0 else math.nan,
  UnaryOps.EXP2: hook_overflow(math.inf, taylor_exp2),
  UnaryOps.SQRT: lambda x: math.sqrt(x) if x >= 0 else math.nan,
  UnaryOps.SIN: taylor_sin,
  UnaryOps.NEG: lambda x: (not x) if isinstance(x, bool) else -x,
  BinaryOps.MUL: operator.mul, BinaryOps.ADD: operator.add, BinaryOps.SUB: operator.sub, BinaryOps.XOR: operator.xor,
  BinaryOps.MAX: max, BinaryOps.CMPEQ: operator.eq, BinaryOps.CMPLT: operator.lt,
  BinaryOps.MOD: lambda x,y: abs(int(x))%abs(int(y))*(1,-1)[x<0],
  BinaryOps.DIV: lambda x,y: int(x/y) if isinstance(x, int) else (x/y if y != 0 else x*math.inf),
  TernaryOps.WHERE: lambda x,y,z: y if x else z}

truncate: Dict[DType, Callable] = {dtypes.bool: bool,
  # TODO: float16 and bfloat16?
  dtypes.float32: lambda x: ctypes.c_float(x).value, dtypes.float64: lambda x: ctypes.c_double(x).value,
  dtypes.uint8: lambda x: ctypes.c_uint8(x).value, dtypes.uint16: lambda x: ctypes.c_uint16(x).value,
  dtypes.uint32: lambda x: ctypes.c_uint32(x).value, dtypes.uint64: lambda x: ctypes.c_uint64(x).value,
  dtypes.int8: lambda x: ctypes.c_int8(x).value, dtypes.int16: lambda x: ctypes.c_int16(x).value,
  dtypes.int32: lambda x: ctypes.c_int32(x).value, dtypes.int64: lambda x: ctypes.c_int64(x).value,}

def exec_alu(op:Op, dtype:DType, operands): return truncate.get(dtype, lambda x: x)(python_alu[op](*operands))
