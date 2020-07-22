from functools import partial
from .base import lib, ffi, GbContainer, GbDelayed, IndexerResolver, AmbiguousAssignOrExtract, Updater, libget
from .scalar import Scalar
from .ops import get_typed_op
from . import dtypes, binary, monoid, semiring
from .mask import StructuralMask, ValueMask
from .exceptions import check_status, is_error, NoValue


class Vector(GbContainer):
    """
    GraphBLAS Sparse Vector
    High-level wrapper around GrB_Vector type
    """
    def __init__(self, gb_obj, dtype):
        super().__init__(gb_obj, dtype)

    def __del__(self):
        check_status(lib.GrB_Vector_free(self.gb_obj))

    def __repr__(self):
        return f'<Vector {self.nvals}/{self.size}:{self.dtype.name}>'

    @property
    def S(self):
        return StructuralMask(self)

    @property
    def V(self):
        return ValueMask(self)

    def __delitem__(self, keys):
        del Updater(self)[keys]

    def __getitem__(self, keys):
        resolved_indexes = IndexerResolver(self, keys)
        return AmbiguousAssignOrExtract(self, resolved_indexes)

    def __setitem__(self, keys, delayed):
        Updater(self)[keys] = delayed

    def isequal(self, other, *, check_dtype=False):
        """
        Check for exact equality (same size, same empty values)
        If `check_dtype` is True, also checks that dtypes match
        For equality of floating point Vectors, consider using `isclose`
        """
        if not isinstance(other, Vector):
            raise TypeError('Argument of isequal must be of type Vector')
        if check_dtype and self.dtype != other.dtype:
            return False
        if self.size != other.size:
            return False
        if self.nvals != other.nvals:
            return False
        if check_dtype:
            # dtypes are equivalent, so not need to unify
            common_dtype = self.dtype
        else:
            common_dtype = dtypes.unify(self.dtype, other.dtype)

        matches = Vector.new(bool, self.size)
        matches << self.ewise_mult(other, binary.eq[common_dtype])
        # ewise_mult performs intersection, so nvals will indicate mismatched empty values
        if matches.nvals != self.nvals:
            return False

        # Check if all results are True
        result = Scalar.new(bool)
        result << matches.reduce(monoid.land)
        return result.value

    def isclose(self, other, *, rel_tol=1e-7, abs_tol=0.0, check_dtype=False):
        """
        Check for approximate equality (including same size and empty values)
        If `check_dtype` is True, also checks that dtypes match
        Closeness check is equivalent to `abs(a-b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)`
        """
        if not isinstance(other, Vector):
            raise TypeError('Argument of isclose must be of type Vector')
        if check_dtype and self.dtype != other.dtype:
            return False
        if self.size != other.size:
            return False
        if self.nvals != other.nvals:
            return False

        matches = self.ewise_mult(other, binary.isclose(rel_tol, abs_tol)).new(dtype=bool)
        # ewise_mult performs intersection, so nvals will indicate mismatched empty values
        if matches.nvals != self.nvals:
            return False

        # Check if all results are True
        return matches.reduce(monoid.land).value

    @property
    def size(self):
        n = ffi.new('GrB_Index*')
        check_status(lib.GrB_Vector_size(n, self.gb_obj[0]))
        return n[0]

    @property
    def shape(self):
        return (self.size,)

    @property
    def nvals(self):
        n = ffi.new('GrB_Index*')
        check_status(lib.GrB_Vector_nvals(n, self.gb_obj[0]))
        return n[0]

    def clear(self):
        check_status(lib.GrB_Vector_clear(self.gb_obj[0]))

    def resize(self, size):
        check_status(lib.GrB_Vector_resize(self.gb_obj[0], size))

    def to_values(self):
        """
        GrB_Vector_extractTuples
        Extract the indices and values as 2 generators
        """
        indices = ffi.new('GrB_Index[]', self.nvals)
        values = ffi.new(f'{self.dtype.c_type}[]', self.nvals)
        n = ffi.new('GrB_Index*')
        n[0] = self.nvals
        func = libget(f'GrB_Vector_extractTuples_{self.dtype.name}')
        check_status(func(
            indices,
            values,
            n,
            self.gb_obj[0]))
        return tuple(indices), tuple(values)

    def build(self, indices, values, *, dup_op=None, clear=False):
        # TODO: add `size` option once .resize is available
        if not isinstance(indices, (tuple, list)):
            indices = tuple(indices)
        if not isinstance(values, (tuple, list)):
            values = tuple(values)
        if len(indices) != len(values):
            raise ValueError(f'`indices` and `values` have different lengths '
                             f'{len(indices)} != {len(values)}')
        if clear:
            self.clear()
        n = len(indices)
        if n <= 0:
            return

        dup_op_given = dup_op is not None
        if not dup_op_given:
            dup_op = binary.plus
        dup_op = get_typed_op(dup_op, self.dtype)
        if dup_op.opclass != 'BinaryOp':
            raise TypeError(f'dup_op must be BinaryOp')

        indices = ffi.new('GrB_Index[]', indices)
        values = ffi.new(f'{self.dtype.c_type}[]', values)
        # Push values into w
        func = libget(f'GrB_Vector_build_{self.dtype.name}')
        check_status(func(
            self.gb_obj[0],
            indices,
            values,
            n,
            dup_op.gb_obj))
        # Check for duplicates when dup_op was not provided
        if not dup_op_given and self.nvals < len(values):
            raise ValueError('Duplicate indices found, must provide `dup_op` BinaryOp')

    def dup(self, *, dtype=None, mask=None):
        """
        GrB_Vector_dup
        Create a new Vector by duplicating this one
        """
        if dtype is not None or mask is not None:
            if dtype is None:
                dtype = self.dtype
            new_vec = self.__class__.new(dtype, size=self.size)
            new_vec(mask=mask)[:] << self
            return new_vec
        new_vec = ffi.new('GrB_Vector*')
        check_status(lib.GrB_Vector_dup(new_vec, self.gb_obj[0]))
        return self.__class__(new_vec, self.dtype)

    @classmethod
    def new(cls, dtype, size=0):
        """
        GrB_Vector_new
        Create a new empty Vector from the given type and size
        """
        new_vector = ffi.new('GrB_Vector*')
        dtype = dtypes.lookup(dtype)
        check_status(lib.GrB_Vector_new(new_vector, dtype.gb_type, size))
        return cls(new_vector, dtype)

    @classmethod
    def from_values(cls, indices, values, *, size=None, dup_op=None, dtype=None):
        """Create a new Vector from the given lists of indices and values.  If
        size is not provided, it is computed from the max index found.
        """
        if not isinstance(indices, (tuple, list)):
            indices = tuple(indices)
        if not isinstance(values, (tuple, list)):
            values = tuple(values)
        if dtype is None:
            if len(values) <= 0:
                raise ValueError('No values provided. Unable to determine type.')
            # Find dtype from any of the values (assumption is they are the same type)
            dtype = type(values[0])
        dtype = dtypes.lookup(dtype)
        # Compute size if not provided
        if size is None:
            if not indices:
                raise ValueError('No indices provided. Unable to infer size.')
            size = max(indices) + 1
        # Create the new vector
        w = cls.new(dtype, size)
        # Add the data
        w.build(indices, values, dup_op=dup_op)
        return w

    #########################################################
    # Delayed methods
    #
    # These return a GbDelayed object which must be passed
    # to update to trigger a call to GraphBLAS
    #########################################################

    def ewise_add(self, other, op=monoid.plus, *, require_monoid=True):
        """
        GrB_eWiseAdd_Vector

        Result will contain the union of indices from both Vectors
        Default op is monoid.plus
        Unless explicitly disabled, this method requires a monoid (directly or from a semiring).
            The reason for this is that binary operators can create very confusing behavior when only
            one of the two elements is present.
            Examples: binary.minus where left=Missing and right=4 yields 4 rather than -4 as might be expected
                      binary.gt where left=Missing and right=4 yields True
                      binary.gt where left=Missing and right=0 yields False
            The behavior is caused by grabbing the non-empty value and using it directly without performing
            any operation. In the case of `gt`, the non-empty value is cast to a boolean.
            For these reasons, users are required to be explicit when choosing this surprising behavior.
        """
        if not isinstance(other, Vector):
            raise TypeError(f'Expected Vector, found {type(other)}')
        op = get_typed_op(op, self.dtype, other.dtype)
        if op.opclass not in {'BinaryOp', 'Monoid', 'Semiring'}:
            raise TypeError(f'op must be BinaryOp, Monoid, or Semiring')
        if require_monoid and op.opclass not in {'Monoid', 'Semiring'}:
            raise TypeError(f'op must be Monoid or Semiring unless require_monoid is False')
        func = libget(f'GrB_eWiseAdd_Vector_{op.opclass}')
        output_constructor = partial(Vector.new,
                                     dtype=op.return_type,
                                     size=self.size)
        return GbDelayed(func,
                         [op.gb_obj, self.gb_obj[0], other.gb_obj[0]],
                         output_constructor=output_constructor,
                         objects=(self, other, op))

    def ewise_mult(self, other, op=binary.times):
        """
        GrB_eWiseMult_Vector

        Result will contain the intersection of indices from both Vectors
        Default op is binary.times
        """
        if not isinstance(other, Vector):
            raise TypeError(f'Expected Vector, found {type(other)}')
        op = get_typed_op(op, self.dtype, other.dtype)
        if op.opclass not in {'BinaryOp', 'Monoid', 'Semiring'}:
            raise TypeError(f'op must be BinaryOp, Monoid, or Semiring')
        func = libget(f'GrB_eWiseMult_Vector_{op.opclass}')
        output_constructor = partial(Vector.new,
                                     dtype=op.return_type,
                                     size=self.size)
        return GbDelayed(func,
                         [op.gb_obj, self.gb_obj[0], other.gb_obj[0]],
                         output_constructor=output_constructor,
                         objects=(self, other, op))

    def vxm(self, other, op=semiring.plus_times):
        """
        GrB_vxm
        Vector-Matrix multiplication. Result is a Vector.
        Default op is semiring.plus_times
        """
        from .matrix import Matrix, TransposedMatrix
        if not isinstance(other, (Matrix, TransposedMatrix)):
            raise TypeError(f'Expected Matrix, found {type(other)}')
        op = get_typed_op(op, self.dtype, other.dtype)
        if op.opclass != 'Semiring':
            raise TypeError(f'op must be Semiring')
        output_constructor = partial(Vector.new,
                                     dtype=op.return_type,
                                     size=other.ncols)
        return GbDelayed(lib.GrB_vxm,
                         [op.gb_obj, self.gb_obj[0], other.gb_obj[0]],
                         bt=other._is_transposed,
                         output_constructor=output_constructor,
                         objects=(self, other, op))

    def apply(self, op, left=None, right=None):
        """
        GrB_Vector_apply
        Apply UnaryOp to each element of the calling Vector
        A BinaryOp can also be applied if a scalar is passed in as `left` or `right`,
            effectively converting a BinaryOp into a UnaryOp
        """
        # This doesn't yet take into account the dtype of left or right (if provided)
        op = get_typed_op(op, self.dtype)
        if op.opclass == 'UnaryOp':
            if left is not None or right is not None:
                raise TypeError('Cannot provide `left` or `right` for a UnaryOp')
        elif op.opclass == 'BinaryOp':
            if left is None and right is None:
                raise TypeError('Must provide either `left` or `right` for a BinaryOp')
            elif left is not None and right is not None:
                raise TypeError('Cannot provide both `left` and `right`')
        else:
            raise TypeError('apply only accepts UnaryOp or BinaryOp')
        output_constructor = partial(Vector.new,
                                     dtype=op.return_type,
                                     size=self.size)
        if op.opclass == 'UnaryOp':
            func = lib.GrB_Vector_apply
            call_args = [op.gb_obj, self.gb_obj[0]]
        else:
            if left is not None:
                if isinstance(left, Scalar):
                    dtype = left.dtype
                    left = left.value
                else:
                    dtype = dtypes.lookup(type(left))
                func = libget(f'GrB_Vector_apply_BinaryOp1st_{dtype}')
                call_args = [op.gb_obj, ffi.cast(dtype.c_type, left), self.gb_obj[0]]
            elif right is not None:
                if isinstance(right, Scalar):
                    dtype = right.dtype
                    right = right.value
                else:
                    dtype = dtypes.lookup(type(right))
                func = libget(f'GrB_Vector_apply_BinaryOp2nd_{dtype}')
                call_args = [op.gb_obj, self.gb_obj[0], ffi.cast(dtype.c_type, right)]

        return GbDelayed(func, call_args, output_constructor=output_constructor, objects=(self, op))

    def reduce(self, op=monoid.plus):
        """
        GrB_Vector_reduce
        Reduce all values into a scalar
        Default op is monoid.lor for boolean and monoid.plus otherwise
        """
        op = get_typed_op(op, self.dtype)
        if op.opclass != 'Monoid':
            raise TypeError(f'op must be Monoid')
        func = libget(f'GrB_Vector_reduce_{op.return_type}')
        output_constructor = partial(Scalar.new,
                                     dtype=op.return_type)
        return GbDelayed(func,
                         [op.gb_obj, self.gb_obj[0]],
                         output_constructor=output_constructor,
                         objects=(self, op))

    ##################################
    # Extract and Assign index methods
    ##################################
    def _extract_element(self, resolved_indexes):
        index, _ = resolved_indexes.indices[0]
        func = libget(f'GrB_Vector_extractElement_{self.dtype}')
        result = ffi.new(f'{self.dtype.c_type}*')

        err_code = func(result,
                        self.gb_obj[0],
                        index)
        # Don't raise error for no value, simply return `None`
        if is_error(err_code, NoValue):
            return None, self.dtype
        check_status(err_code)
        return result[0], self.dtype

    def _prep_for_extract(self, resolved_indexes):
        index, isize = resolved_indexes.indices[0]
        output_constructor = partial(Vector.new,
                                     dtype=self.dtype,
                                     size=isize)
        return GbDelayed(lib.GrB_Vector_extract,
                         [self.gb_obj[0], index, isize],
                         output_constructor=output_constructor,
                         objects=self)

    def _assign_element(self, resolved_indexes, value):
        index, _ = resolved_indexes.indices[0]
        func = libget(f'GrB_Vector_setElement_{self.dtype}')
        check_status(func(
                     self.gb_obj[0],
                     ffi.cast(self.dtype.c_type, value),
                     index))

    def _prep_for_assign(self, resolved_indexes, obj):
        index, isize = resolved_indexes.indices[0]
        if isinstance(obj, Scalar):
            obj = obj.value
        if isinstance(obj, (int, float, bool, complex)):
            dtype = self.dtype
            func = libget(f'GrB_Vector_assign_{dtype.name}')
            scalar = ffi.cast(dtype.c_type, obj)
            delayed = GbDelayed(func,
                                [scalar, index, isize],
                                objects=self)
        elif isinstance(obj, Vector):
            delayed = GbDelayed(lib.GrB_Vector_assign,
                                [obj.gb_obj[0], index, isize],
                                objects=(self, obj))
        else:
            raise TypeError(f'Unexpected type for assignment value: {type(obj)}')
        return delayed

    def _delete_element(self, resolved_indexes):
        index, _ = resolved_indexes.indices[0]
        check_status(lib.GrB_Vector_removeElement(
                     self.gb_obj[0],
                     index))
