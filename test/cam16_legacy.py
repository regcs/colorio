# -*- coding: utf-8 -*-
#
from __future__ import division

import numpy

from colorio.illuminants import whitepoints_cie1931
from colorio.linalg import dot, solve


class CAM16Legacy(object):
    '''
    Legacy CAM16 implementation for comparison purposes.
    '''
    # pylint: disable=too-many-instance-attributes, too-many-arguments
    def __init__(self, c, Y_b, L_A, exact_inversion=True,
                 whitepoint=whitepoints_cie1931['D65']):
        # step0: Calculate all values/parameters which are independent of input
        #        samples
        Y_w = whitepoint[1]

        # Nc and F are modelled as a function of c, and can be linearly
        # interpolated.
        c_vals = [0.525, 0.59, 0.69]  # 0.525 vs. 0.535 in CIECAM02
        F_Nc_vals = [0.8, 0.9, 1.0]
        assert 0.525 <= c <= 0.69
        F = numpy.interp(c, c_vals, F_Nc_vals)
        self.c = c
        self.N_c = F

        self.M16 = numpy.array([
            [+0.401288, +0.650173, -0.051461],
            [-0.250268, +1.204414, +0.045854],
            [-0.002079, +0.048952, +0.953127],
            ])
        # The standard actually recommends using this approximation as
        # inversion operation.
        approx_inv_M16 = numpy.array([
            [+1.86206786, -1.01125463, +0.14918677],
            [+0.38752654, +0.62144744, -0.00897398],
            [-0.01584150, -0.03412294, +1.04996444],
            ])
        self.solve_M16 = (
            (lambda x: solve(self.M16, x)) if exact_inversion else
            (lambda x: dot(approx_inv_M16, x))
            )
        RGB_w = numpy.dot(self.M16, whitepoint)

        D = F * (1 - 1/3.6 * numpy.exp((-L_A-42)/92))
        D = min(D, 1.0)
        D = max(D, 0.0)

        self.D_RGB = D*Y_w/RGB_w + 1 - D

        k = 1 / (5*L_A + 1)
        self.F_L = k**4 * L_A + 0.1*(1-k**4)**2 * numpy.cbrt(5*L_A)

        self.n = Y_b / Y_w
        self.z = 1.48 + numpy.sqrt(self.n)
        self.N_bb = 0.725 / self.n**0.2
        self.N_cb = self.N_bb

        RGB_wc = self.D_RGB * RGB_w
        alpha = (self.F_L*RGB_wc/100)**0.42
        RGB_aw_ = 400 * alpha / (alpha + 27.13) + 0.1
        self.A_w = (numpy.dot([2, 1, 1/20], RGB_aw_) - 0.305) * self.N_bb

        self.h = numpy.array([20.14, 90.00, 164.25, 237.53, 380.14])
        self.e = numpy.array([0.8, 0.7, 1.0, 1.2, 0.8])
        self.H = numpy.array([0.0, 100.0, 200.0, 300.0, 400.0])
        return

    def from_xyz100(self, xyz):
        # Step 1: Calculate 'cone' responses
        rgb = dot(self.M16, xyz)
        # Step 2: Complete the color adaptation of the illuminant in
        #         the corresponding cone response space
        rgb_c = (rgb.T * self.D_RGB).T

        # Step 3: Calculate the post-adaptation cone response (resulting in
        #         dynamic range compression)
        alpha = (self.F_L*abs(rgb_c)/100)**0.42
        rgb_a = numpy.sign(rgb_c) * 400 * alpha / (alpha+27.13) + 0.1

        # Step 4
        a = dot(numpy.array([1, -12/11, 1/11]), rgb_a)
        b = dot(numpy.array([1/9, 1/9, -2/9]), rgb_a)
        # Make sure that h is in [0, 360]
        h = numpy.rad2deg(numpy.arctan2(b, a)) % 360

        # Step 5: Calculate eccentricity (e_t) and hue composition (H), using
        #         the unique hue data given in Table 2.4.
        h_ = (h - self.h[0]) % 360 + self.h[0]
        e_t = (numpy.cos(numpy.deg2rad(h_) + 2) + 3.8) / 4
        i = numpy.searchsorted(self.h, h_) - 1
        beta = (h_ - self.h[i]) * self.e[i+1]
        H = self.H[i] + 100 * beta / (beta + self.e[i]*(self.h[i+1] - h_))

        # Step 6
        A = (dot(numpy.array([2, 1, 1/20]), rgb_a) - 0.305) * self.N_bb

        # Step 7: Calculate the correlate of lightness
        J = 100 * (A/self.A_w)**(self.c*self.z)

        # Step 8: Calculate the correlate of brightness
        sqrt_J_100 = numpy.sqrt(J/100)
        Q = (4/self.c) * sqrt_J_100 * (self.A_w + 4) * self.F_L**0.25

        # Step 9: Calculate the correlates of chroma (C), colourfulness (M)
        #          and saturation (s)
        #
        t = 50000/13 * e_t * self.N_c * self.N_cb * numpy.sqrt(a**2 + b**2) \
            / dot(numpy.array([1, 1, 21/20]), rgb_a)
        C = t**0.9 * (1.64 - 0.29**self.n)**0.73 * sqrt_J_100
        M = C * self.F_L**0.25
        s = 100 * numpy.sqrt(M/Q)

        return numpy.array([J, C, H, h, M, s, Q])

    def to_xyz100(self, data, description):
        '''Input: J or Q; C, M or s; H or h
        '''
        if description[0] == 'J':
            J = data[0]
            # Q perhaps needed for C
            Q = (4/self.c) * numpy.sqrt(J/100) * (self.A_w+4) * self.F_L**0.25
        else:
            # Step 1–1: Compute J from Q (if start from Q)
            assert description[0] == 'Q'
            Q = data[0]
            J = 6.25 * (self.c*Q / (self.A_w+4) / self.F_L**0.25)**2

        # Step 1–2: Calculate C from M or s
        if description[1] == 'C':
            C = data[1]
        elif description[1] == 'M':
            M = data[1]
            C = M / self.F_L**0.25
        else:
            assert description[1] == 's'
            s = data[1]
            C = (s/100)**2 * Q / self.F_L**0.25

        if description[2] == 'h':
            h = data[2]
        else:
            assert description[2] == 'H'
            # Step 1–3: Calculate h from H (if start from H)
            H = data[2]
            i = numpy.searchsorted(self.H, H) - 1
            Hi = self.H[i]
            hi, hi1 = self.h[i], self.h[i+1]
            ei, ei1 = self.e[i], self.e[i+1]
            h_ = ((H - Hi) * (ei1*hi - ei*hi1) - 100*hi*ei1) \
                / ((H - Hi) * (ei1 - ei) - 100*ei1)
            h = numpy.mod(h_, 360)

        h = numpy.deg2rad(h)

        # Step 2: Calculate t, et , p1, p2 and p3
        A = self.A_w * (J/100)**(1/self.c/self.z)

        # Step 3: Calculate a and b
        t = (C / numpy.sqrt(J/100) / (1.64 - 0.29**self.n)**0.73)**(1/0.9)
        e_t = 0.25 * (numpy.cos(h + 2) + 3.8)

        one_over_t = 1 / t
        one_over_t = numpy.select(
            [numpy.isnan(one_over_t), True], [numpy.inf, one_over_t]
            )

        p1 = (50000. / 13) * self.N_c * self.N_cb * e_t * one_over_t
        p2 = A / self.N_bb + 0.305
        p3 = 21 / 20

        sin_h = numpy.sin(h)
        cos_h = numpy.cos(h)

        num = p2 * (2 + p3) * (460. / 1403)
        denom_part2 = (2 + p3) * (220. / 1403)
        denom_part3 = (-27. / 1403) + p3 * (6300. / 1403)

        a = numpy.empty_like(h)
        b = numpy.empty_like(h)

        small_cos = (numpy.abs(sin_h) >= numpy.abs(cos_h))
        b[small_cos] = (
            num[small_cos] / (
                p1[small_cos] / sin_h[small_cos]
                + (denom_part2 * cos_h[small_cos] / sin_h[small_cos])
                + denom_part3
            ))
        a[small_cos] = b[small_cos] * cos_h[small_cos] / sin_h[small_cos]
        a[~small_cos] = (
                num[~small_cos] / (
                    p1[~small_cos] / cos_h[~small_cos]
                    + denom_part2
                    + (denom_part3 * sin_h[~small_cos] / cos_h[~small_cos])
                    ))
        b[~small_cos] = a[~small_cos] * sin_h[~small_cos] / cos_h[~small_cos]

        # Step 4: Calculate RGB_a_
        rgb_a_ = dot(numpy.array([
            [460, 451, 288],
            [460, -891, -261],
            [460, -220, -6300]
            ]), numpy.array([p2, a, b])) / 1403

        # Step 5: Calculate RGB_
        rgb_ = numpy.sign(rgb_a_ - 0.1) * 100/self.F_L * (
            (27.13 * abs(rgb_a_ - 0.1)) / (400 - abs(rgb_a_ - 0.1))
            )**(1/0.42)

        rgb_c = rgb_

        # Step 6: Calculate R, G and B
        rgb = (rgb_c.T / self.D_RGB).T

        # Step 7: Calculate X, Y and Z
        xyz = self.solve_M16(rgb)
        return xyz
