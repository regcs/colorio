# -*- coding: utf-8 -*-
#
from __future__ import print_function, division

import os

from dolfin import (
    Mesh,
    MeshEditor,
    FunctionSpace,
    Function,
    grad,
    VectorFunctionSpace,
    project,
    TrialFunction,
    TestFunction,
    dot,
    dx,
    assemble,
    Expression,
    as_backend_type,
    BoundingBoxTree,
    Point,
    Cell,
)
import numpy
import pyamg
from scipy import sparse
from scipy.sparse.linalg import LinearOperator
from scipy.optimize import leastsq, least_squares
import yaml

import meshzoo

from pade2d import Pade2d


def f_ellipse(a_b_theta, x):
    a, b, theta = a_b_theta
    cos = numpy.cos(theta)
    sin = numpy.sin(theta)
    return (
        +a ** 2 * (x[0] * cos + x[1] * sin) ** 2
        + b ** 2 * (x[0] * sin - x[1] * cos) ** 2
        - 1.0
    )


def jac_ellipse(a_b_theta, x):
    a, b, theta = a_b_theta
    cos = numpy.cos(theta)
    sin = numpy.sin(theta)
    return numpy.array(
        [
            +2 * a * (x[0] * cos + x[1] * sin) ** 2,
            #
            +2 * b * (x[0] * sin - x[1] * cos) ** 2,
            #
            +a ** 2 * 2 * (x[0] * cos + x[1] * sin) * (-x[0] * sin + x[1] * cos)
            + b ** 2 * 2 * (x[0] * sin - x[1] * cos) * (+x[0] * cos + x[1] * sin),
        ]
    ).T


def _get_luo_rigg():
    dir_path = os.path.dirname(os.path.realpath(__file__))
    with open(os.path.join(dir_path, "../colorio/data/luo-rigg/luo-rigg.yaml")) as f:
        data = yaml.safe_load(f)

    centers = []
    J = []
    for _, data_set in data.items():
        for _, dat in data_set.items():
            x, y, Y, a, ab, theta, _ = dat
            a /= 1.0e4
            a *= (Y / 30) ** 0.2
            b = a / ab

            centers.append([x, y])

            J.append(
                numpy.array(
                    [
                        [a * numpy.cos(theta), -b * numpy.sin(theta)],
                        [a * numpy.sin(theta), b * numpy.cos(theta)],
                    ]
                )
            )

    return numpy.array(centers), numpy.moveaxis(numpy.array(J), 0, -1)


def _get_macadam():
    dir_path = os.path.dirname(os.path.realpath(__file__))
    with open(os.path.join(dir_path, "../colorio/data/macadam1942/table3.yaml")) as f:
        data = yaml.safe_load(f)

    centers = []
    points = []
    for datak in data:
        # collect ellipse points
        _, _, _, _, delta_y_delta_x, delta_s = numpy.array(datak["data"]).T
        if len(delta_s) < 2:
            continue
        center = [datak["x"], datak["y"]]
        centers.append(center)
        offset = (
            numpy.array([numpy.ones(delta_y_delta_x.shape[0]), delta_y_delta_x])
            / numpy.sqrt(1 + delta_y_delta_x ** 2)
            * delta_s
        )
        points.append(
            numpy.column_stack([(center + offset.T).T, (center - offset.T).T])
        )

    centers = numpy.array(centers)
    J = get_local_linearizations1(centers, points)
    return centers, numpy.moveaxis(J, 0, -1)
    # return centers, self.get_local_linearizations2(centers, points)


def get_local_linearizations1(centers, points):
    # Get ellipse parameters
    X = [(pts.T - center).T for center, pts in zip(centers, points)]
    a_b_theta = numpy.array(
        [
            # Solve least squares problem for [1/a, 1/b, theta]
            # and pick [a, b, theta]
            leastsq(
                lambda a_b_theta: f_ellipse(a_b_theta, x),
                [1.0, 1.0, 0.0],
                Dfun=lambda a_b_theta: jac_ellipse(a_b_theta, x),
            )[0]
            for x in X
        ]
    )
    a_b_theta = numpy.array(
        [1 / a_b_theta[:, 0], 1 / a_b_theta[:, 1], a_b_theta[:, 2]]
    ).T
    # Construct 2x2 matrices that approximately convert unit circles into
    # the ellipse defined by the points.
    J = []
    for abt in a_b_theta:
        a, b, theta = abt
        J.append(
            numpy.array(
                [
                    [a * numpy.cos(theta), -b * numpy.sin(theta)],
                    [a * numpy.sin(theta), b * numpy.cos(theta)],
                ]
            )
        )

    return numpy.array(J)


def get_local_linearizations2(centers, points):
    X = [(pts.T - center).T for center, pts in zip(centers, points)]

    def f_linear_function(j, x):
        Jx = numpy.dot(j.reshape(2, 2), x)
        out = numpy.einsum("ij,ij->j", Jx, Jx) - 1.0
        return out

    def jac_linear_function(j, x):
        J = j.reshape(2, 2)
        return numpy.array(
            [
                2 * J[0, 0] * x[0] ** 2 + 2 * J[0, 1] * x[0] * x[1],
                2 * J[0, 1] * x[1] ** 2 + 2 * J[0, 0] * x[0] * x[1],
                2 * J[1, 0] * x[0] ** 2 + 2 * J[1, 1] * x[0] * x[1],
                2 * J[1, 1] * x[1] ** 2 + 2 * J[1, 0] * x[0] * x[1],
            ]
        ).T

    J = []
    for x in X:
        j, _ = leastsq(
            lambda J: f_linear_function(J, x),
            [1.0, 0.0, 0.0, 1.0],
            Dfun=lambda J: jac_linear_function(J, x),
            # full_output=True
        )
        J.append(numpy.linalg.inv(j.reshape(2, 2)))

    return numpy.array(J)


class PadeEllipse(object):
    def __init__(self, centers, J, degrees):
        self.centers = centers
        self.J = J

        self.target = 0.002
        self.J /= self.target

        self.num_f_eval = 0

        self.degrees = degrees

        num_coefficients = [
            (degrees[0] + 1) * (degrees[0] + 2) // 2,
            (degrees[1] + 1) * (degrees[1] + 2) // 2,
            (degrees[2] + 1) * (degrees[2] + 2) // 2,
            (degrees[3] + 1) * (degrees[3] + 2) // 2,
        ]

        # Choose the coefficiens to create the identity function
        ax = numpy.zeros(num_coefficients[0])
        ax[1] = 1
        bx = numpy.zeros(num_coefficients[1] - 1)
        ay = numpy.zeros(num_coefficients[2])
        ay[2] = 1
        by = numpy.zeros(num_coefficients[3] - 1)

        self.alpha = numpy.concatenate([ax, bx, ay, by])

        bx = numpy.concatenate([[1.0], bx])
        by = numpy.concatenate([[1.0], by])

        self.pade2d = Pade2d(self.centers.T, degrees, ax, bx, ay, by)

        # self.J = numpy.array(self.get_local_linearizations2(centers, points))

        # # plot
        # for center, pts, j in zip(centers, points, self.J):
        #     # plot points
        #     p = (pts.T - center).T
        #     plt.plot(*p, '.')
        #     # plot circle
        #     t = numpy.linspace(0.0, 2.0*numpy.pi, 1000)
        #     xy = numpy.array([numpy.cos(t), numpy.sin(t)])
        #     plt.plot(*numpy.dot(j, xy), '-', label='ellipse')
        #     plt.legend()
        #     # # plot transformation
        #     # xy_new = numpy.dot(j, p)
        #     # plt.plot(*xy_new, 'x')
        #     plt.axis('equal')
        #     plt.show()
        return

    def _set_alpha(self, alpha):
        # Subtract 1 for each denominator polynomial since the constant
        # coefficient is fixed to 1.0.
        assert len(alpha) == len(self.alpha)

        self.alpha = alpha

        num_coefficients = [(d + 1) * (d + 2) // 2 for d in self.degrees]
        num_coefficients[1] -= 1
        num_coefficients[3] -= 1

        ax, bx, ay, by = numpy.split(alpha, numpy.cumsum(num_coefficients[:-1]))
        bx = numpy.concatenate([[1.0], bx])
        by = numpy.concatenate([[1.0], by])

        self.pade2d.set_coefficients(ax, bx, ay, by)
        return

    def get_q2_r2(self, alpha):
        self._set_alpha(alpha)

        # jacs and J are of shape (2, 2, k). M must be of the same shape and
        # contain the result of the k 2x2 dot products. Perhaps there's a
        # dot() for this.
        M = numpy.einsum("ijl,jkl->ikl", self.pade2d.jac(), self.J)

        # One could use
        #
        #     M = numpy.moveaxis(M, -1, 0)
        #     _, sigma, _ = numpy.linalg.svd(M)
        #
        # but computing the singular values explicitly via
        # <https://scicomp.stackexchange.com/a/14103/3980> is faster.
        a = (M[0, 0] + M[1, 1]) / 2
        b = (M[0, 0] - M[1, 1]) / 2
        c = (M[1, 0] + M[0, 1]) / 2
        d = (M[1, 0] - M[0, 1]) / 2

        # From the square roots of q2 and r2, the ellipse axes can be computed,
        # namely
        #
        #   s1 = q + r
        #   s2 = q - r
        #
        q2 = a ** 2 + d ** 2
        r2 = b ** 2 + c ** 2

        return q2, r2

    def get_ellipse_axes(self, alpha):
        q, r = numpy.sqrt(self.get_q2_r2(alpha))
        sigma = numpy.array([q + r, q - r]) * self.target
        return sigma

    def cost(self, alpha):
        q2, r2 = self.get_q2_r2(alpha)

        out = numpy.array([q2 - 1.0, r2]).flatten()

        self.num_f_eval += 1
        if self.num_f_eval % 10000 == 0:
            cost = numpy.sum(out ** 2)
            print("{:7d}     {}".format(self.num_f_eval, cost))
        return out


def build_grad_matrices(V, points):
    """Build the sparse m-by-n matrices that map a coefficient set for a function in V
    to the values of dx and dy at a number m of points.
    """
    # See <https://www.allanswered.com/post/lkbkm/#zxqgk>
    mesh = V.mesh()

    bbt = BoundingBoxTree()
    bbt.build(mesh)
    dofmap = V.dofmap()
    el = V.element()
    rows = []
    cols = []
    datax = []
    datay = []
    for i, xy in enumerate(points):
        cell_id = bbt.compute_first_entity_collision(Point(*xy))
        cell = Cell(mesh, cell_id)
        coordinate_dofs = cell.get_vertex_coordinates()

        rows.append([i, i, i])
        cols.append(dofmap.cell_dofs(cell_id))

        v = el.evaluate_basis_derivatives_all(1, xy, coordinate_dofs, cell_id)
        v = v.reshape(3, 2)
        datax.append(v[:, 0])
        datay.append(v[:, 1])

    rows = numpy.concatenate(rows)
    cols = numpy.concatenate(cols)
    datax = numpy.concatenate(datax)
    datay = numpy.concatenate(datay)

    m = len(points)
    n = V.dim()
    dx_matrix = sparse.csr_matrix((datax, (rows, cols)), shape=(m, n))
    dy_matrix = sparse.csr_matrix((datay, (rows, cols)), shape=(m, n))
    return dx_matrix, dy_matrix


class PiecewiseEllipse(object):
    def __init__(self, centers, J, n):
        self.centers = centers
        self.J = J

        self.target = 0.002
        self.J /= self.target

        # dir_path = os.path.dirname(os.path.realpath(__file__))
        # with open(os.path.join(dir_path, '../colorio/data/gamut_triangulation.yaml')) as f:
        #     data = yaml.safe_load(f)

        # self.points = numpy.column_stack([
        #     data['points'], numpy.zeros(len(data['points']))
        #     ])
        # self.cells = numpy.array(data['cells'])

        # self.points, self.cells = colorio.xy_gamut_mesh(0.15)

        self.points, self.cells = meshzoo.triangle(
            n, corners=numpy.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        )

        # https://bitbucket.org/fenics-project/dolfin/issues/845/initialize-mesh-from-vertices
        editor = MeshEditor()
        mesh = Mesh()
        editor.open(mesh, "triangle", 2, 2)
        editor.init_vertices(self.points.shape[0])
        editor.init_cells(self.cells.shape[0])
        for k, point in enumerate(self.points):
            editor.add_vertex(k, point[:2])
        for k, cell in enumerate(self.cells):
            editor.add_cell(k, cell)
        editor.close()

        self.V = FunctionSpace(mesh, "CG", 1)
        self.Vgrad = VectorFunctionSpace(mesh, "DG", 0)

        # self.ux0 = Function(self.V)
        # self.uy0 = Function(self.V)

        # Use F(x, y) = (x, y) as starting guess
        self.ux0 = project(Expression("x[0]", degree=1), self.V)
        self.uy0 = project(Expression("x[1]", degree=1), self.V)
        ax = self.ux0.vector().get_local()
        ay = self.uy0.vector().get_local()
        # 0 starting guess
        # ax = numpy.zeros(self.V.dim())
        # ay = numpy.zeros(self.V.dim())
        self.alpha = numpy.concatenate([ax, ay])

        self.num_f_eval = 0

        # Build L as scipy.csr_matrix
        u = TrialFunction(self.V)
        v = TestFunction(self.V)
        L = assemble(dot(grad(u), grad(v)) * dx)
        Lmat = as_backend_type(L).mat()
        indptr, indices, data = Lmat.getValuesCSR()
        # Set the first row to [1.0, 0.0, ..., 0.0] to emulate a Dirichlet
        # condition. This setting makes sure that the "first" node maps to [0,
        # 0], a convention. Useful because now, L is nonsingular. Unfortunately
        # it's also nonsymmetric.
        data[indptr[0] : indptr[1]] = 0.0
        assert indices[0] == 0
        data[0] = 1.0
        size = Lmat.getSize()
        self.L = sparse.csr_matrix((data, indices, indptr), shape=size)
        self.LT = self.L.getH()

        self.dx, self.dy = build_grad_matrices(self.V, centers)
        self.dxT = self.dx.getH()
        self.dyT = self.dy.getH()
        return

    def apply_M(self, ax, ay):
        """Linear operator that converts ax, ay to abcd.
        """
        jac = numpy.array(
            [[self.dx.dot(ax), self.dy.dot(ax)], [self.dx.dot(ay), self.dy.dot(ay)]]
        )

        # jacs and J are of shape (2, 2, k). M must be of the same shape and
        # contain the result of the k 2x2 dot products. Perhaps there's a
        # dot() for this.
        M = numpy.einsum("ijl,jkl->ikl", jac, self.J)
        # M = numpy.array([
        #     [
        #         jac[0][0]*self.J[0][0] + jac[0][1]*self.J[1][0],
        #         jac[0][0]*self.J[0][1] + jac[0][1]*self.J[1][1],
        #     ],
        #     [
        #         jac[1][0]*self.J[0][0] + jac[1][1]*self.J[1][0],
        #         jac[1][0]*self.J[0][1] + jac[1][1]*self.J[1][1],
        #     ],
        #     ])

        # One could use
        #
        #     M = numpy.moveaxis(M, -1, 0)
        #     _, sigma, _ = numpy.linalg.svd(M)
        #
        # but computing the singular values explicitly via
        # <https://scicomp.stackexchange.com/a/14103/3980> is faster and more
        # explicit.
        a = (M[0, 0] + M[1, 1]) / 2
        b = (M[0, 0] - M[1, 1]) / 2
        c = (M[1, 0] + M[0, 1]) / 2
        d = (M[1, 0] - M[0, 1]) / 2

        return a, b, c, d

    def apply_M_alt(self, ax, ay):
        X = numpy.array(
            [self.dx.dot(ax), self.dy.dot(ax), self.dx.dot(ay), self.dy.dot(ay)]
        )

        Y = numpy.array(
            [
                X[0] * self.J[0][0] + X[1] * self.J[1][0],
                X[0] * self.J[0][1] + X[1] * self.J[1][1],
                X[2] * self.J[0][0] + X[3] * self.J[1][0],
                X[2] * self.J[0][1] + X[3] * self.J[1][1],
            ]
        )

        Z = 0.5 * numpy.array([Y[0] + Y[3], Y[0] - Y[3], Y[2] + Y[1], Y[2] - Y[1]])
        return Z

    def apply_MT(self, abcd):
        a, b, c, d = abcd
        X = 0.5 * numpy.array([a + b, c - d, c + d, a - b])

        Y = numpy.array(
            [
                X[0] * self.J[0][0] + X[1] * self.J[0][1],
                X[0] * self.J[1][0] + X[1] * self.J[1][1],
                X[2] * self.J[0][0] + X[3] * self.J[0][1],
                X[2] * self.J[1][0] + X[3] * self.J[1][1],
            ]
        )

        Z = numpy.array(
            [
                self.dxT.dot(Y[0]) + self.dyT.dot(Y[1]),
                self.dxT.dot(Y[2]) + self.dyT.dot(Y[3]),
            ]
        )
        return Z

    def get_q2_r2(self, ax, ay):
        a, b, c, d = self.apply_M(ax, ay)
        # From the square roots of q2 and r2, the ellipse axes can be computed,
        # namely
        #
        #   s1 = q + r
        #   s2 = q - r
        #
        q2 = a ** 2 + d ** 2
        r2 = b ** 2 + c ** 2
        return q2, r2

    def jac_q2_r2(self, ax, ay, bx, by):
        a, b, c, d = self.apply_M(ax, ay)
        #
        e, f, g, h = self.apply_M(bx, by)
        out1 = 2 * (a * e + d * h)
        out2 = 2 * (b * f + c * g)
        return out1, out2

    def jacT_q2_r2(self, ax, ay, out1, out2):
        a, b, c, d = self.apply_M(ax, ay)
        #
        X = 2 * numpy.array([a * out1, b * out2, c * out2, d * out1])
        Y = self.apply_MT(X)
        return Y

    def get_ellipse_axes(self, alpha):
        ax, ay = numpy.split(alpha, 2)
        q, r = numpy.sqrt(self.get_q2_r2(ax, ay))
        sigma = numpy.array([q + r, q - r]) * self.target
        return sigma

    def cost_ls(self, alpha):
        n = self.V.dim()
        ax = alpha[:n]
        ay = alpha[n:]

        # res_x, res_y = self.L.dot(numpy.column_stack([ax, ay])).T
        res_x = self.L.dot(ax)
        res_y = self.L.dot(ay)

        q2, r2 = self.get_q2_r2(ax, ay)

        # Some word on the (absence of) weights here.
        # Weights on the residuals are not required: The residual entries are
        # integrals with the test functions, so they'll naturally decrease in
        # absolute value as the cell size decreases.
        # One idea for scaling q2 and r2 would be to divide by the number of
        # measurement points (or rather the sqrt thereof). This would ensure
        # that, if more measure points are added, they as a set aren't weighted
        # more than the other quality indicators, e.g., the smoothness in x and
        # y.  On the other hand, by omitting an explicit weight that depends on
        # the number of data points, one asserts that additional measurements
        # do not decrease the weights on the other measurements. As
        # consequence, more measurements as a set take a higher relative weight
        # in the cost function. This is what we want.
        out = numpy.array([res_x, res_y, q2 - 1.0, r2])

        self.num_f_eval += 1
        if self.num_f_eval % 100 == 0:
            cost = numpy.array([numpy.dot(ot, ot) for ot in out])
            print("{:7d}     {:e} {:e} {:e} {:e}".format(self.num_f_eval, *cost))

        return numpy.concatenate(out)

    def jac_ls(self, alpha):
        m = 2 * self.V.dim() + 2 * self.centers.shape[0]
        n = alpha.shape[0]

        d = self.V.dim()
        c = self.centers.shape[0]
        assert 2 * d == n

        ax = alpha[:d]
        ay = alpha[d:]
        jac_alpha = numpy.array(
            [[self.dx.dot(ax), self.dy.dot(ax)], [self.dx.dot(ay), self.dy.dot(ay)]]
        )
        M_alpha = numpy.einsum("ijl,jkl->ikl", jac_alpha, self.J)
        a_alpha = (M_alpha[0, 0] + M_alpha[1, 1]) / 2
        b_alpha = (M_alpha[0, 0] - M_alpha[1, 1]) / 2
        c_alpha = (M_alpha[1, 0] + M_alpha[0, 1]) / 2
        d_alpha = (M_alpha[1, 0] - M_alpha[0, 1]) / 2

        def matvec(phi):
            if len(phi.shape) > 1:
                assert len(phi.shape) == 2
                assert phi.shape[1] == 1
                phi = phi[:, 0]

            # Laplace part (it's linear, so this is easy)
            ax = phi[:d]
            ay = phi[d:]
            res_x = self.L.dot(ax)
            res_y = self.L.dot(ay)

            # q2, r2 part
            jac_phi = numpy.array(
                [[self.dx.dot(ax), self.dy.dot(ax)], [self.dx.dot(ay), self.dy.dot(ay)]]
            )
            M_phi = numpy.einsum("ijl,jkl->ikl", jac_phi, self.J)
            a_phi = M_phi[0, 0] + M_phi[1, 1]
            b_phi = M_phi[0, 0] - M_phi[1, 1]
            c_phi = M_phi[1, 0] + M_phi[0, 1]
            d_phi = M_phi[1, 0] - M_phi[0, 1]
            dq2_phi = a_alpha * a_phi + d_alpha * d_phi
            dr2_phi = b_alpha * b_phi + c_alpha * c_phi

            return numpy.concatenate([res_x, res_y, dq2_phi, dr2_phi])

        def rmatvec(vec):
            res_x = vec[:d]
            res_y = vec[d : 2 * d]
            dq2_phi = vec[2 * d : 2 * d + c]
            dr2_phi = vec[2 * d + c :]

            X = numpy.array(
                [
                    a_alpha * dq2_phi,
                    b_alpha * dr2_phi,
                    c_alpha * dr2_phi,
                    d_alpha * dq2_phi,
                ]
            )
            Y = numpy.array([X[0] + X[1], X[2] - X[3], X[2] + X[3], X[0] - X[1]])
            Z = numpy.array(
                [
                    self.J[0][0] * Y[0] + self.J[0][1] * Y[1],
                    self.J[1][0] * Y[0] + self.J[1][1] * Y[1],
                    self.J[0][0] * Y[2] + self.J[0][1] * Y[3],
                    self.J[1][0] * Y[2] + self.J[1][1] * Y[3],
                ]
            )

            return numpy.concatenate(
                [
                    self.LT.dot(res_x) + self.dxT.dot(Z[0]) + self.dyT.dot(Z[1]),
                    self.LT.dot(res_y) + self.dxT.dot(Z[2]) + self.dyT.dot(Z[3]),
                ]
            )

        # # test matvec
        # u = alpha
        # numpy.random.seed(0)
        # du = numpy.random.rand(n)
        # # du = numpy.zeros(n)
        # # du[0] = 1.0
        # eps = 1.0e-10
        # fupdu = self.cost(u + eps*du)
        # fumdu = self.cost(u - eps*du)
        # fu = self.cost(u)
        # ndiff1 = (fupdu - fu) / eps
        # ndiff2 = (fu - fumdu) / eps
        # ndiff3 = (fupdu - fumdu) / (2*eps)
        # jdiff1 = matvec(du)
        # jdiff2 = numpy.dot(matrix, du)
        # print()
        # d = self.V.dim()
        # print(ndiff1[-4:])
        # print(ndiff2[-4:])
        # print(ndiff3[-4:])
        # print(jdiff1[-4:])
        # print(jdiff2[-4:])
        # print()

        return LinearOperator([m, n], matvec=matvec, rmatvec=rmatvec)

    def cost_min(self, alpha):
        n = self.V.dim()
        ax = alpha[:n]
        ay = alpha[n:]

        Lax = self.L * ax
        Lay = self.L * ay

        q2, r2 = self.get_q2_r2(ax, ay)

        out = [
            0.5 * numpy.dot(Lax, Lax),
            0.5 * numpy.dot(Lay, Lay),
            0.5 * numpy.dot(q2 - 1, q2 - 1),
            0.5 * numpy.dot(r2, r2),
        ]

        if self.num_f_eval % 10000 == 0:
            print("{:7d}     {:e} {:e} {:e} {:e}".format(self.num_f_eval, *out))

        self.num_f_eval += 1
        return numpy.sum(out)

    def grad_min(self, alpha):
        n = self.V.dim()

        assert_equality = False

        if assert_equality:
            M = []
            for k in range(30):
                e = numpy.zeros(30)
                e[k] = 1.0
                ax = e[:n]
                ay = e[n:]
                M.append(numpy.concatenate(self.apply_M_alt(ax, ay)))
            M = numpy.column_stack(M)

            MT = []
            for k in range(100):
                e = numpy.zeros(100)
                e[k] = 1.0
                abcd = numpy.array([e[:25], e[25:50], e[50:75], e[75:]])
                MT.append(numpy.concatenate(self.apply_MT(abcd)))
            MT = numpy.column_stack(MT)
            assert numpy.all(abs(M.T - MT) < 1.0e-13)

        if assert_equality:
            M = []
            for k in range(30):
                e = numpy.zeros(30)
                e[k] = 1.0
                bx = e[:n]
                by = e[n:]
                M.append(numpy.concatenate(self.jac_q2_r2(ax, ay, bx, by)))
            M = numpy.column_stack(M)

            MT = []
            for k in range(50):
                e = numpy.zeros(50)
                e[k] = 1.0
                out1 = e[:25]
                out2 = e[25:]
                MT.append(numpy.concatenate(self.jacT_q2_r2(ax, ay, out1, out2)))
            MT = numpy.column_stack(MT)
            assert numpy.all(abs(M.T - MT) < 1.0e-13)

        ax = alpha[:n]
        ay = alpha[n:]

        q2, r2 = self.get_q2_r2(ax, ay)
        j = self.jacT_q2_r2(ax, ay, q2 - 1, r2)

        out = [self.LT.dot(self.L.dot(ax)) + j[0], self.LT.dot(self.L.dot(ay)) + j[1]]

        if assert_equality:
            n = len(alpha)
            g = []
            for k in range(n):
                e = numpy.zeros(n)
                e[k] = 1.0
                eps = 1.0e-5
                f0 = self.cost_min(alpha - eps * e)
                f1 = self.cost_min(alpha + eps * e)
                g.append((f1 - f0) / (2 * eps))

            # print(numpy.array(g))
            # print(numpy.concatenate(out))
            assert numpy.all(abs(numpy.array(g) - numpy.concatenate(out)) < 1.0e-5)

        return numpy.concatenate(out)

    def cost_min2(self, alpha):
        """Residual formulation, Hessian is a low-rank update of the identity.
        """
        n = self.V.dim()
        ax = alpha[:n]
        ay = alpha[n:]

        # ml = pyamg.ruge_stuben_solver(self.L)
        # # ml = pyamg.smoothed_aggregation_solver(self.L)
        # print(ml)
        # print()
        # print(self.L)
        # print()
        # x = ml.solve(ax, tol=1e-10)
        # print('residual: {}'.format(numpy.linalg.norm(ax - self.L*x)))
        # print()
        # print(ax)
        # print()
        # print(x)
        # exit(1)

        # x = sparse.linalg.spsolve(self.L, ax)
        # print('residual: {}'.format(numpy.linalg.norm(ax - self.L*x)))
        # exit(1)

        q2, r2 = self.get_q2_r2(ax, ay)

        Lax = self.L * ax
        Lay = self.L * ay

        out = [
            0.5 * numpy.dot(Lax, Lax),
            0.5 * numpy.dot(Lay, Lay),
            0.5 * numpy.dot(q2 - 1, q2 - 1),
            0.5 * numpy.dot(r2, r2),
        ]

        if self.num_f_eval % 10000 == 0:
            print("{:7d}     {:e} {:e} {:e} {:e}".format(self.num_f_eval, *out))

        self.num_f_eval += 1
        return numpy.sum(out)

    def get_u(self, alpha):
        n = self.V.dim()
        ax = alpha[:n]
        ay = alpha[n:]

        ux = Function(self.V)
        ux.vector().set_local(ax)
        ux.vector().apply("")

        uy = Function(self.V)
        uy.vector().set_local(ay)
        uy.vector().apply("")
        return ux, uy


def _main():
    centers, J = _get_macadam()
    # centers, J = _get_luo_rigg()

    # problem = PadeEllipse(centers, J, [2, 0, 2, 0])
    n = 32
    problem = PiecewiseEllipse(centers, J, n)

    print("num parameters: {}".format(len(problem.alpha)))

    alpha0 = problem.alpha.copy()

    # Levenberg-Marquardt (lm) is better suited for small, dense, unconstrained
    # problems, but it needs more conditions than parameters. This is not the case for
    # larger polynomial degrees.
    print("f evals     cost")
    # out = least_squares(
    #     problem.cost_ls, alpha0,
    #     jac=problem.jac_ls,
    #     max_nfev=10000,
    #     method='trf',
    #     # tr_solver='exact',
    #     tr_solver='lsmr',
    #     )
    # print('{:7d}'.format(out.nfev))

    from scipy.optimize import show_options

    # print(show_options(solver='minimize', method='cg'))
    from scipy.optimize import minimize

    out = minimize(problem.cost_min, alpha0, jac=problem.grad_min, method="L-BFGS-B")
    print(out.success)
    print(out.fun)
    print(out.nfev)

    filename = "optimal-{}.npy".format(n)
    print("Writing data to {}".format(filename))
    numpy.save(filename, {"n": n, "data": out.x})
    return


if __name__ == "__main__":
    _main()
