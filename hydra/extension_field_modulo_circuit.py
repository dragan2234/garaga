from hydra.modulo_circuit import (
    ModuloCircuit,
    ModuloCircuitElement,
    WriteOps,
    ModBuiltinOps,
)
from hydra.algebra import BaseField, PyFelt, Polynomial
from hydra.poseidon_transcript import CairoPoseidonTranscript
from hydra.hints.extf_mul import (
    nondeterministic_extension_field_mul_divmod,
    nondeterministic_extension_field_div,
)
from hydra.definitions import (
    get_irreducible_poly,
    CurveID,
    STARK,
    CURVES,
    Curve,
    N_LIMBS,
)

from dataclasses import dataclass, field, InitVar
from pprint import pprint
from random import randint
from enum import Enum
import functools


POSEIDON_BUILTIN_SIZE = 6
POSEIDON_OUTPUT_S1_INDEX = 4


# Represents the state of the accumulation of the equation
#  c_i * Π(Pi(z)) = c_i*Q_i*P + c_i*R_i inside the circuit.
# Only store ci*Π(Pi(z)) (as Emulated Field Element) and ci*R_i (as Polynomial)
@dataclass(slots=True)
class EuclideanPolyAccumulator:
    lhs: ModuloCircuitElement
    R: list[ModuloCircuitElement]
    R_evaluated: ModuloCircuitElement


class AccPolyInstructionType(Enum):
    MUL = "MUL"
    DIV = "DIV"
    SQUARE_TORUS = "SQUARE_TORUS"


@dataclass(slots=True)
class AccumulatePolyInstructions:
    Pis: list[list[list[ModuloCircuitElement]]] = field(default_factory=list)
    Qis: list[Polynomial] = field(default_factory=list)
    Ris: list[list[ModuloCircuitElement]] = field(default_factory=list)
    Ps_sparsities: list[None | list[list[int]]] = field(default_factory=list)
    r_sparsities: list[None | list[int]] = field(default_factory=list)
    types: list[AccPolyInstructionType] = field(default_factory=list)
    n: int = field(default=0)
    rlc_coeffs: list[ModuloCircuitElement] = field(default_factory=list)
    Pis_of_Z: list[ModuloCircuitElement] = field(default_factory=list)

    def append(
        self,
        type: AccPolyInstructionType,
        Pis: list[list[ModuloCircuitElement]],
        Q: Polynomial,
        R: list[ModuloCircuitElement],
        Ps_sparsities: None | list[list[int] | None] = None,
        r_sparsity: None | list[int] = None,
    ):
        if type != AccPolyInstructionType.MUL:
            assert len(Pis) == 2
        self.types.append(type)
        self.Pis.append(Pis)
        self.Qis.append(Q)
        self.Ris.append(R)
        self.Ps_sparsities.append(Ps_sparsities)
        self.r_sparsities.append(r_sparsity)
        self.n += 1


class ExtensionFieldModuloCircuit(ModuloCircuit):
    def __init__(
        self,
        name: str,
        curve_id: int,
        extension_degree: int,
        init_hash: int = None,
        hash_input: bool = True,
        compilation_mode: int = 0,
    ) -> None:
        super().__init__(
            name=name, curve_id=curve_id, compilation_mode=compilation_mode
        )
        self.class_name = "ExtensionFieldModuloCircuit"
        self.extension_degree = extension_degree
        self.z_powers: list[ModuloCircuitElement] = []
        self.acc: list[EuclideanPolyAccumulator] = [
            self._init_accumulator(self.extension_degree),
            self._init_accumulator(self.extension_degree * 2),
        ]
        self.hash_input = hash_input
        self.transcript: CairoPoseidonTranscript = CairoPoseidonTranscript(
            init_hash=(
                int.from_bytes(self.name.encode(), "big")
                if init_hash is None
                else init_hash
            )
        )
        self.z_powers: list[ModuloCircuitElement] = []
        self.ops_counter = {
            "Circuit": self.name,
            "EXTF_SQUARE": 0,
            "EXTF_MUL_DENSE": 0,
            # "EXTF_DIV": 0,
        }
        self.accumulate_poly_instructions: list[AccumulatePolyInstructions] = [
            AccumulatePolyInstructions(),
            AccumulatePolyInstructions(),
        ]

    def _init_accumulator(self, extension_degree: int = None):
        extension_degree = extension_degree or self.extension_degree
        return EuclideanPolyAccumulator(
            lhs=None,
            R=[None] * extension_degree,
            R_evaluated=None,
        )

    @property
    def commitments(self):
        return [
            self.values_segment.segment_stacks[WriteOps.COMMIT][offset].felt
            for offset in sorted(self.values_segment.segment_stacks[WriteOps.COMMIT])
        ]

    @property
    def circuit_input(self):
        return [
            self.values_segment.segment_stacks[WriteOps.INPUT][offset].felt
            for offset in sorted(self.values_segment.segment_stacks[WriteOps.INPUT])
        ]

    def create_powers_of_Z(
        self, Z: PyFelt, mock: bool = False, max_degree: int = None
    ) -> list[ModuloCircuitElement]:
        if max_degree is None:
            max_degree = self.extension_degree
        powers = [self.write_cairo_native_felt(Z)]
        if not mock:
            for _ in range(2, max_degree + 1):
                powers.append(self.mul(powers[-1], powers[0]))
        else:
            powers = powers + [
                self.write_element(
                    self.field(Z.value**i), write_source=WriteOps.WITNESS
                )
                for i in range(2, max_degree + 1)
            ]
        self.z_powers = powers
        return powers

    def eval_poly_in_precomputed_Z(
        self, X: list[ModuloCircuitElement], sparsity: list[int] = None
    ) -> ModuloCircuitElement:
        """
        Evaluates a polynomial with coefficients `X` at precomputed powers of z.
        X(z) = X_0 + X_1 * z + X_2 * z^2 + ... + X_n * z^n

        If a `sparsity` list is provided, it is used to optimize the evaluation by skipping
        zero coefficients and applying special handling based on sparsity values:
        - A sparsity value of 0 indicates that the corresponding coefficient X_i is zero and should be skipped.
        - A sparsity value of 1 indicates that the corresponding coefficient X_i is non-zero and should be included in the evaluation.
        - A sparsity value of 2 indicates a special case where the term is directly a power of z, implying the coefficient X_i is 1 in this case.

        Parameters:
        - X: list[ModuloCircuitElement] - The coefficients of the polynomial to be evaluated.
        - sparsity: list[int] (optional) - A list indicating the sparsity of the polynomial. If None, the polynomial is treated as fully dense.

        Returns:
        - ModuloCircuitElement: The result of evaluating the polynomial at the precomputed powers of z.
        """
        assert len(X) - 1 <= len(
            self.z_powers
        ), f"Degree {len(X)-1} > Zpowlen = {len(self.z_powers)}"

        if sparsity:
            first_non_zero_idx = next(
                (i for i, x in enumerate(sparsity) if x != 0), None
            )
            X_of_z = (
                X[first_non_zero_idx]
                if first_non_zero_idx == 0
                else self.mul(
                    X[first_non_zero_idx], self.z_powers[first_non_zero_idx - 1]
                )
            )
            for i in range(first_non_zero_idx + 1, len(X)):
                match sparsity[i]:
                    case 1:
                        term = self.mul(X[i], self.z_powers[i - 1])
                    case 2:
                        term = self.z_powers[
                            i - 1
                        ]  # In this case, sparsity[i] == 2 => X[i] = 1
                    case _:
                        continue
                X_of_z = self.add(X_of_z, term)
        else:
            X_of_z = self.eval_poly(X, self.z_powers)

        return X_of_z

    def extf_add(
        self, X: list[ModuloCircuitElement], Y: list[ModuloCircuitElement]
    ) -> list[ModuloCircuitElement]:
        """
        Adds two polynomials with coefficients `X` and `Y`.
        Returns R = [x0 + y0, x1 + y1, x2 + y2, ... + xn-1 + yn-1] mod p
        """
        assert len(X) == len(Y), f"len(X)={len(X)} != len(Y)={len(Y)}"
        return [self.add(x_i, y_i) for x_i, y_i in zip(X, Y)]

    def extf_scalar_mul(
        self, X: list[ModuloCircuitElement], c: ModuloCircuitElement
    ) -> list[ModuloCircuitElement]:
        """
        Multiplies a polynomial with coefficients `X` by a scalar `c`.
        Input : I(x) = i0 + i1*x + i2*x^2 + ... + in-1*x^n-1
        Output : O(x) = ci0 + ci1*x + ci2*x^2 + ... + cin-1*x^n-1.
        This is done in the circuit.
        """
        assert type(c) == ModuloCircuitElement
        return [self.mul(x_i, c) for x_i in X]

    def extf_neg(self, X: list[ModuloCircuitElement]) -> list[ModuloCircuitElement]:
        """
        Negates a polynomial with coefficients `X`.
        Returns R = [-x0, -x1, -x2, ... -xn-1] mod p
        """
        return [self.neg(x_i) for x_i in X]

    def extf_sub(
        self, X: list[ModuloCircuitElement], Y: list[ModuloCircuitElement]
    ) -> list[ModuloCircuitElement]:
        return [self.sub(x, y) for x, y in zip(X, Y)]

    def extf_mul(
        self,
        Ps: list[list[ModuloCircuitElement]],
        extension_degree: int,
        Ps_sparsities: list[list[int] | None] = None,
        r_sparsity: list[int] = None,
        acc_index: int = 0,
    ) -> list[ModuloCircuitElement]:
        """
        Multiply in the extension field X * Y mod irreducible_poly
        Commit to R and add an EvalPolyInstruction to the accumulator.
        """
        assert (
            extension_degree > 2
        ), f"extension_degree={extension_degree} <= 2. Use self.mul or self.fp2_square instead."

        if Ps_sparsities is None:
            Ps_sparsities = [None] * len(Ps)
        assert len(Ps_sparsities) == len(
            Ps
        ), f"len(Ps_sparsities)={len(Ps_sparsities)} != len(Ps)={len(Ps)}"

        Q, R = nondeterministic_extension_field_mul_divmod(
            Ps, self.curve_id, extension_degree
        )

        R = self.write_elements(R, WriteOps.COMMIT, r_sparsity)

        if not any(sparsity for sparsity in Ps_sparsities) or not r_sparsity:
            self.ops_counter["EXTF_MUL_DENSE"] += 1

        self.accumulate_poly_instructions[acc_index].append(
            AccPolyInstructionType.MUL,
            Ps,
            Polynomial(Q),
            R,
            Ps_sparsities,
            r_sparsity,
        )
        return R

    def extf_div(
        self,
        X: list[ModuloCircuitElement],
        Y: list[ModuloCircuitElement],
        extension_degree: int,
        acc_index: int = 0,
    ) -> list[ModuloCircuitElement]:
        x_over_y = nondeterministic_extension_field_div(
            X, Y, self.curve_id, extension_degree
        )
        x_over_y = self.write_elements(x_over_y, WriteOps.COMMIT)

        Q, _ = nondeterministic_extension_field_mul_divmod(
            [x_over_y, Y], self.curve_id, extension_degree
        )
        # R should be X
        Q = Polynomial(Q)
        self.accumulate_poly_instructions[acc_index].append(
            AccPolyInstructionType.DIV, Pis=[x_over_y, Y], Q=Q, R=X
        )
        return x_over_y

    def extf_inv(
        self,
        Y: list[ModuloCircuitElement],
        extension_degree: int,
        acc_index: int = 0,
    ) -> list[ModuloCircuitElement]:
        one = [ModuloCircuitElement(self.field(1), -1)] + [
            ModuloCircuitElement(self.field(0), -1)
        ] * (extension_degree - 1)
        y_inv = nondeterministic_extension_field_div(
            one,
            Y,
            self.curve_id,
            extension_degree,
        )
        y_inv = self.write_elements(y_inv, WriteOps.COMMIT)

        Q, _ = nondeterministic_extension_field_mul_divmod(
            [y_inv, Y], self.curve_id, extension_degree
        )
        # R should be One. Passed at mocked modulo circuits element since fully determined by its sparsity.
        Q = Polynomial(Q)
        self.accumulate_poly_instructions[acc_index].append(
            AccPolyInstructionType.DIV,
            Pis=[y_inv, Y],
            Q=Q,
            R=one,
            r_sparsity=[2] + [0] * (extension_degree - 1),
        )
        return y_inv

    def update_LHS_state(
        self,
        s1: PyFelt,
        Ps: list[list[ModuloCircuitElement]],
        Ps_sparsities: list[list[int] | None] = None,
        acc_index: int = 0,
    ):
        # Sanity checks for sparsities
        Ps_sparsities = Ps_sparsities or [None] * len(Ps)
        assert len(Ps_sparsities) == len(
            Ps
        ), f"len(Ps_sparsities)={len(Ps_sparsities)} != len(Ps)={len(Ps)}"

        for i, sparsity in enumerate(Ps_sparsities):
            if sparsity:
                assert all(
                    Ps[i][j].value == 0 for j in range(len(Ps[i])) if sparsity[j] == 0
                )
                assert all(
                    Ps[i][j].value == 1 for j in range(len(Ps[i])) if sparsity[j] == 2
                )

        # Evaluate LHS = Π(Pi(z)) inside circuit.
        # i=0
        LHS = self.eval_poly_in_precomputed_Z(Ps[0], Ps_sparsities[0])
        LHS_current_eval = LHS
        # Keep P0(z)
        self.accumulate_poly_instructions[acc_index].Pis_of_Z.append([])
        self.accumulate_poly_instructions[acc_index].Pis_of_Z[-1].append(LHS)
        for i in range(1, len(Ps)):
            if Ps[i - 1] == Ps[i]:
                # Consecutives elements are the same : Squaring
                LHS_current_eval = LHS_current_eval

            else:
                LHS_current_eval = self.eval_poly_in_precomputed_Z(
                    Ps[i], Ps_sparsities[i]
                )
            # Keep Pi(z)
            self.accumulate_poly_instructions[acc_index].Pis_of_Z[-1].append(
                LHS_current_eval
            )
            # Update LHS
            LHS = self.mul(LHS, LHS_current_eval)

        ci_XY_of_z = self.mul(s1, LHS)

        LHS_acc = self.add(self.acc[acc_index].lhs, ci_XY_of_z)

        # Update LHS only.
        self.acc[acc_index] = EuclideanPolyAccumulator(
            lhs=LHS_acc,
            R=self.acc[acc_index].R,
            R_evaluated=self.acc[acc_index].R_evaluated,
        )

        return

    def update_RHS_state(
        self,
        type: AccPolyInstructionType,
        s1: ModuloCircuitElement,
        R: list[ModuloCircuitElement],
        r_sparsity: list[int] = None,
        acc_index: int = 0,
        instruction_index: int = 0,
    ):

        # Find if R_of_z is already computed in the first Pi of the next instruction.
        # In this case we avoid accumulating R as polynomial and can use directly R_of_z with the correct s1.
        if type != AccPolyInstructionType.SQUARE_TORUS:
            if instruction_index + 1 < self.accumulate_poly_instructions[acc_index].n:
                if (
                    self.accumulate_poly_instructions[acc_index].Pis[
                        instruction_index + 1
                    ][0]
                    == R
                ):
                    already_computed_R_of_z = self.accumulate_poly_instructions[
                        acc_index
                    ].Pis_of_Z[instruction_index + 1][0]

                    # Update direclty Rhs_evaluted
                    self.acc[acc_index] = EuclideanPolyAccumulator(
                        lhs=self.acc[acc_index].lhs,
                        R=self.acc[acc_index].R,
                        R_evaluated=self.add(
                            self.acc[acc_index].R_evaluated,
                            self.mul(s1, already_computed_R_of_z),
                        ),
                    )
                    return

        # If not found, computes R_acc = R_acc + s1 * R as a Polynomial inside circuit
        if r_sparsity:
            if type != AccPolyInstructionType.SQUARE_TORUS:
                # Sanity check is already done in square_torus function.
                # If the instruction is SQUARE_TORUS, then R is fully determined by its sparsity, R(x) = x.
                # Actual R value here is the SQ TORUS result.
                # See square torus function for this edge case.
                assert all(R[i].value == 0 for i in range(len(R)) if r_sparsity[i] == 0)
            R_acc = []
            for i, (r_acc, r) in enumerate(zip(self.acc[acc_index].R, R)):
                match r_sparsity[i]:
                    case 1:
                        R_acc.append(self.add(r_acc, self.mul(s1, r)))
                    case 2:
                        R_acc.append(self.add(r_acc, s1))
                    case _:
                        R_acc.append(r_acc)

        else:
            # Computes R_acc = R_acc + s1 * R without sparsity info.
            R_acc = [
                self.add(r_acc, self.mul(s1, r))
                for r_acc, r in zip(self.acc[acc_index].R, R)
            ]
        # Update accumulator state
        self.acc[acc_index] = EuclideanPolyAccumulator(
            lhs=self.acc[acc_index].lhs,
            R=R_acc,
            R_evaluated=self.acc[acc_index].R_evaluated,
        )
        return

    def get_Z_and_nondeterministic_Q(
        self, extension_degree: int, mock: bool = False
    ) -> tuple[PyFelt, tuple[list[PyFelt], list[PyFelt]]]:
        nondeterministic_Qs = [Polynomial([self.field.zero()]) for _ in range(2)]
        # Start by hashing circuit input
        if self.hash_input:
            self.transcript.hash_limbs_multi(self.circuit_input)

        double_extension = self.accumulate_poly_instructions[1].n > 0

        # Compute Random Linear Combination coefficients
        acc_indexes = [0, 1] if double_extension else [0]

        for acc_index in acc_indexes:
            for i, instruction_type in enumerate(
                self.accumulate_poly_instructions[acc_index].types
            ):
                # print(f"{i=}, Hashing {instruction_type}")
                match instruction_type:
                    case AccPolyInstructionType.MUL:
                        self.transcript.hash_limbs_multi(
                            self.accumulate_poly_instructions[acc_index].Ris[i],
                            self.accumulate_poly_instructions[acc_index].r_sparsities[
                                i
                            ],
                        )
                    case AccPolyInstructionType.SQUARE_TORUS:
                        self.transcript.hash_limbs_multi(
                            self.accumulate_poly_instructions[acc_index].Ris[i]
                        )

                    case AccPolyInstructionType.DIV:
                        self.transcript.hash_limbs_multi(
                            self.accumulate_poly_instructions[acc_index].Pis[i][0],
                        )

                    case _:
                        raise ValueError(
                            f"Unknown instruction type: {instruction_type}"
                        )

                self.accumulate_poly_instructions[acc_index].rlc_coeffs.append(
                    self.write_cairo_native_felt(self.field(self.transcript.RLC_coeff))
                )
            # Computes Q = Σ(ci * Qi)
            for i, coeff in enumerate(
                self.accumulate_poly_instructions[acc_index].rlc_coeffs
            ):
                nondeterministic_Qs[acc_index] += (
                    self.accumulate_poly_instructions[acc_index].Qis[i] * coeff
                )

            # Extend Q with zeros if needed to match the expected degree.
            nondeterministic_Qs[acc_index] = nondeterministic_Qs[acc_index].get_coeffs()
            nondeterministic_Qs[acc_index] = nondeterministic_Qs[acc_index] + [
                self.field.zero()
            ] * (
                (acc_index + 1) * extension_degree
                - 1
                - len(nondeterministic_Qs[acc_index])
            )
        # HASH(COMMIT0, COMMIT1, Q0, Q1)
        # Add Q to transcript to get Z.
        if not mock:
            self.transcript.hash_limbs_multi(nondeterministic_Qs[0])
            if double_extension:
                self.transcript.hash_limbs_multi(nondeterministic_Qs[1])

        Z = self.field(self.transcript.continuable_hash)

        return (Z, nondeterministic_Qs)

    def finalize_circuit(
        self,
        extension_degree: int = None,
        mock=False,
    ):
        # print("\n Finalize Circuit")
        extension_degree = extension_degree or self.extension_degree

        z, Qs = self.get_Z_and_nondeterministic_Q(extension_degree, mock)
        compute_z_up_to = max(max(len(Qs[0]), len(Qs[1])) - 1, extension_degree)
        # print(f"{self.name} compute_z_up_to: {compute_z_up_to}")

        Q = [self.write_elements(Qs[0], WriteOps.COMMIT)]
        double_extension = self.accumulate_poly_instructions[1].n > 0

        if double_extension:
            Q.append(self.write_elements(Qs[1], WriteOps.COMMIT))
            compute_z_up_to = max(compute_z_up_to, extension_degree * 2)

        self.create_powers_of_Z(z, mock=mock, max_degree=compute_z_up_to)

        acc_indexes = [0, 1] if double_extension else [0]

        for acc_index in acc_indexes:
            for i in range(self.accumulate_poly_instructions[acc_index].n):
                self.update_LHS_state(
                    s1=self.accumulate_poly_instructions[acc_index].rlc_coeffs[i],
                    Ps=self.accumulate_poly_instructions[acc_index].Pis[i],
                    Ps_sparsities=self.accumulate_poly_instructions[
                        acc_index
                    ].Ps_sparsities[i],
                    acc_index=acc_index,
                )

            for i in range(self.accumulate_poly_instructions[acc_index].n):
                self.update_RHS_state(
                    type=self.accumulate_poly_instructions[acc_index].types[i],
                    s1=self.accumulate_poly_instructions[acc_index].rlc_coeffs[i],
                    R=self.accumulate_poly_instructions[acc_index].Ris[i],
                    r_sparsity=self.accumulate_poly_instructions[
                        acc_index
                    ].r_sparsities[i],
                    acc_index=acc_index,
                    instruction_index=i,
                )

            if not mock:
                Q_of_Z = self.eval_poly_in_precomputed_Z(Q[acc_index])
                P, sparsity = self.write_sparse_elements(
                    get_irreducible_poly(
                        self.curve_id, (acc_index + 1) * extension_degree
                    ).get_coeffs(),
                    WriteOps.CONSTANT,
                )
                P_of_z = P[0]
                sparse_p_index = 1
                for i in range(1, len(sparsity)):
                    if sparsity[i] == 1:
                        P_of_z = self.add(
                            P_of_z, self.mul(P[sparse_p_index], self.z_powers[i - 1])
                        )
                        sparse_p_index += 1

                R_of_Z = self.eval_poly_in_precomputed_Z(self.acc[acc_index].R)

                lhs = self.acc[acc_index].lhs
                rhs = self.add(
                    self.mul(Q_of_Z, P_of_z),
                    self.add(R_of_Z, self.acc[acc_index].R_evaluated),
                )
                assert (
                    lhs.value == rhs.value
                ), f"{lhs.value} != {rhs.value}, {acc_index}"
                if self.compilation_mode == 0:
                    self.sub_and_assert(
                        lhs, rhs, self.set_or_get_constant(self.field.zero())
                    )
                else:
                    eq_check = self.sub(rhs, lhs)
                    self.extend_output([eq_check])

        return True

    def summarize(self):
        add_count, mul_count, assert_eq_count = self.values_segment.summarize()
        summary = {
            "circuit": self.name,
            "MULMOD": mul_count,
            "ADDMOD": add_count,
            "ASSERT_EQ": assert_eq_count,
            "POSEIDON": (
                self.transcript.permutations_count
                if self.transcript.permutations_count > 1
                else 0
            ),
            "RLC": self.accumulate_poly_instructions[0].n
            + self.accumulate_poly_instructions[1].n,
        }

        return summary

    def compile_circuit(self, function_name: str = None):
        self.values_segment = self.values_segment.non_interactive_transform()
        if self.compilation_mode == 0:
            return self.compile_circuit_cairo_zero(function_name), None
        elif self.compilation_mode == 1:
            return self.compile_circuit_cairo_1(function_name)

    def compile_circuit_cairo_zero(
        self,
        function_name: str = None,
        returns: dict[str] = {
            "felt*": [
                "constants_ptr",
                "add_offsets_ptr",
                "mul_offsets_ptr",
                "output_offsets_ptr",
                "poseidon_indexes_ptr",
            ],
            "felt": [
                "constants_ptr_len",
                "input_len",
                "commitments_len",
                "witnesses_len",
                "output_len",
                "continuous_output",
                "add_mod_n",
                "mul_mod_n",
                "n_assert_eq",
                "N_Euclidean_equations",
                "name",
                "curve_id",
            ],
        },
    ) -> str:
        dw_arrays = self.values_segment.get_dw_lookups()
        dw_arrays["poseidon_indexes_ptr"] = self.transcript.poseidon_ptr_indexes
        name = function_name or self.values_segment.name
        function_name = f"get_{name}_circuit"
        code = f"func {function_name}()->(circuit:{self.class_name}*)" + "{" + "\n"

        code += "alloc_locals;\n"
        code += "let (__fp__, _) = get_fp_and_pc();\n"

        for dw_array_name in returns["felt*"]:
            code += f"let ({dw_array_name}:felt*) = get_label_location({dw_array_name}_loc);\n"

        code += f"let constants_ptr_len = {len(dw_arrays['constants_ptr'])};\n"
        code += f"let input_len = {len(self.values_segment.segment_stacks[WriteOps.INPUT])*N_LIMBS};\n"
        code += f"let commitments_len = {len(self.commitments)*N_LIMBS};\n"
        code += f"let witnesses_len = {len(self.values_segment.segment_stacks[WriteOps.WITNESS])*N_LIMBS};\n"
        code += f"let output_len = {len(self.output)*N_LIMBS};\n"
        continuous_output = self.continuous_output
        code += f"let continuous_output = {1 if continuous_output else 0};\n"
        code += f"let add_mod_n = {len(dw_arrays['add_offsets_ptr'])};\n"
        code += f"let mul_mod_n = {len(dw_arrays['mul_offsets_ptr'])};\n"
        code += (
            f"let n_assert_eq = {len(self.values_segment.assert_eq_instructions)};\n"
        )
        code += (
            f"let N_Euclidean_equations = {len(dw_arrays['poseidon_indexes_ptr'])};\n"
        )
        code += f"let name = '{self.name}';\n"
        code += f"let curve_id = {self.curve_id};\n"

        code += f"local circuit:ExtensionFieldModuloCircuit = ExtensionFieldModuloCircuit({', '.join(returns['felt*'])}, {', '.join(returns['felt'])});\n"
        code += f"return (&circuit,);\n"

        for dw_array_name in returns["felt*"]:
            dw_values = dw_arrays[dw_array_name]
            code += f"\t {dw_array_name}_loc:\n"
            if dw_array_name == "constants_ptr":
                for bigint in dw_values:
                    for limb in bigint:
                        code += f"\t dw {limb};\n"
                code += "\n"

            elif dw_array_name in ["add_offsets_ptr", "mul_offsets_ptr"]:
                num_instructions = len(dw_values)
                instructions_needed = (8 - (num_instructions % 8)) % 8
                for left, right, result in dw_values:
                    code += (
                        f"\t dw {left};\n" + f"\t dw {right};\n" + f"\t dw {result};\n"
                    )
                if instructions_needed > 0:
                    first_triplet = dw_values[0]
                    for _ in range(instructions_needed):
                        code += (
                            f"\t dw {first_triplet[0]};\n"
                            + f"\t dw {first_triplet[1]};\n"
                            + f"\t dw {first_triplet[2]};\n"
                        )
                code += "\n"
            elif dw_array_name in ["output_offsets_ptr"]:
                if continuous_output:
                    code += f"\t dw {dw_values[0]};\n"
                else:
                    for val in dw_values:
                        code += f"\t dw {val};\n"

            elif dw_array_name in [
                "poseidon_indexes_ptr",
            ]:
                for val in dw_values:
                    code += (
                        f"\t dw {POSEIDON_BUILTIN_SIZE*val+POSEIDON_OUTPUT_S1_INDEX};\n"
                    )

        code += "\n"
        code += "}\n"
        return code

    def compile_circuit_cairo_1(
        self,
        function_name: str = None,
    ) -> str:
        name = function_name or self.values_segment.name
        function_name = f"get_{name}_circuit"
        curve_index = CurveID.find_value_in_string(name)
        if self.generic_circuit:
            code = (
                f"fn {function_name}(mut input: Array<u384>, curve_index:usize)->Array<u384>"
                + "{"
                + "\n"
            )
        else:
            code = (
                f"fn {function_name}(mut input: Array<u384>)->Array<u384>" + "{" + "\n"
            )

        def write_stack(
            write_ops: WriteOps,
            code: str,
            offset_to_reference_map: dict[int, str],
            start_index: int,
        ) -> tuple:
            if len(self.values_segment.segment_stacks[write_ops]) > 0:
                code += f"\n // {write_ops.name} stack\n"
                for i, offset in enumerate(
                    self.values_segment.segment_stacks[write_ops].keys()
                ):

                    code += f"\t let in{start_index+i} = CircuitElement::<CircuitInput<{start_index+i}>> {{}}; // {self.values_segment.segment[offset].value if write_ops==WriteOps.CONSTANT else ''}\n"
                    offset_to_reference_map[offset] = f"in{start_index+i}"
                return (
                    code,
                    offset_to_reference_map,
                    start_index + len(self.values_segment.segment_stacks[write_ops]),
                )
            else:
                return code, offset_to_reference_map, start_index

        code, offset_to_reference_map, start_index = write_stack(
            WriteOps.CONSTANT, code, {}, 0
        )

        code, offset_to_reference_map, commit_start_index = write_stack(
            WriteOps.INPUT, code, offset_to_reference_map, start_index
        )
        code, offset_to_reference_map, commit_end_index = write_stack(
            WriteOps.COMMIT, code, offset_to_reference_map, commit_start_index
        )
        code, offset_to_reference_map, start_index = write_stack(
            WriteOps.WITNESS, code, offset_to_reference_map, commit_end_index
        )
        code, offset_to_reference_map, start_index = write_stack(
            WriteOps.FELT, code, offset_to_reference_map, start_index
        )
        for i, (offset, vs_item) in enumerate(
            self.values_segment.segment_stacks[WriteOps.BUILTIN].items()
        ):
            op = vs_item.instruction.operation
            left_offset = vs_item.instruction.left_offset
            right_offset = vs_item.instruction.right_offset
            result_offset = vs_item.instruction.result_offset
            # print(op, offset_to_reference_map, left_offset, right_offset, result_offset)
            match op:
                case ModBuiltinOps.ADD:
                    if right_offset > result_offset:
                        # Case sub
                        code += f"let t{i} = circuit_sub({offset_to_reference_map[result_offset]}, {offset_to_reference_map[left_offset]});\n"
                        offset_to_reference_map[offset] = f"t{i}"
                        assert offset == right_offset
                    else:
                        code += f"let t{i} = circuit_add({offset_to_reference_map[left_offset]}, {offset_to_reference_map[right_offset]});\n"
                        offset_to_reference_map[offset] = f"t{i}"
                        assert offset == result_offset

                case ModBuiltinOps.MUL:
                    if right_offset == result_offset == offset:
                        # Case inv
                        # print(f"\t INV {left_offset} {right_offset} {result_offset}")
                        code += f"let t{i} = circuit_inverse({offset_to_reference_map[left_offset]});\n"
                        offset_to_reference_map[offset] = f"t{i}"
                    else:
                        # print(f"MUL {left_offset} {right_offset} {result_offset}")
                        code += f"let t{i} = circuit_mul({offset_to_reference_map[left_offset]}, {offset_to_reference_map[right_offset]});\n"
                        offset_to_reference_map[offset] = f"t{i}"
                        assert offset == result_offset

        outputs_refs = []
        for out in self.output:
            if self.values_segment[out.offset].write_source == WriteOps.BUILTIN:
                outputs_refs.append(offset_to_reference_map[out.offset])
            else:
                continue

        # outputs_refs = [offset_to_reference_map[out.offset] for out in self.output]

        if self.exact_output_refs_needed:
            outputs_refs_needed = [
                offset_to_reference_map[out.offset]
                for out in self.exact_output_refs_needed
            ]
        else:
            outputs_refs_needed = outputs_refs

        code += f"// {commit_start_index=}, {commit_end_index-1=}"
        code += f"""
    let p = get_p({curve_index if curve_index is not None else 'curve_index'});
    let modulus = TryInto::<_, CircuitModulus>::try_into([p.limb0, p.limb1, p.limb2, p.limb3])
        .unwrap();

    let mut circuit_inputs = ({','.join(outputs_refs_needed)},).new_inputs();

    while let Option::Some(val) = input.pop_front() {{
        circuit_inputs = circuit_inputs.next(val);
    }};

    let outputs = match circuit_inputs.done().eval(modulus) {{
        Result::Ok(outputs) => {{ outputs }},
        Result::Err(_) => {{ panic!("Expected success") }}
    }};
"""
        for i, ref in enumerate(outputs_refs):
            code += f"\t let o{i} = outputs.get_output({ref});\n"
        code += "\n"
        code += f"let res=array![{','.join(['o'+str(i) for i, _ in enumerate(outputs_refs)])}];\n"
        code += "return res;\n"
        code += "}\n"
        return code, function_name


if __name__ == "__main__":
    from hydra.definitions import CURVES, CurveID

    def init_z_circuit(z: int = 2):
        c = ExtensionFieldModuloCircuit("test", CurveID.BN254.value, 6)
        c.create_powers_of_Z(c.field(z), mock=True)
        return c

    def test_eval():
        c = init_z_circuit()
        X = c.write_elements(
            [PyFelt(1, c.field.p) for _ in range(6)], operation=WriteOps.INPUT
        )
        print("X(z)", [x.value for x in X])
        X = c.eval_poly_in_precomputed_Z(X)
        print("X(z)", X.value)
        c.print_value_segment()
        print([hex(x.value) for x in c.z_powers], len(c.z_powers))

    test_eval()

    def test_eval_sparse():
        c = init_z_circuit()
        X = c.write_elements(
            [c.field.one(), c.field.zero(), c.field.one()], operation=WriteOps.INPUT
        )
        X = c.eval_poly_in_precomputed_Z(X, sparsity=[1, 0, 1])
        print("X(z)", X.value)
        c.print_value_segment()
        print([hex(x.value) for x in c.z_powers], len(c.z_powers))

    test_eval_sparse()
