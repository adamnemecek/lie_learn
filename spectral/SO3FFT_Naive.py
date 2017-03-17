
import numpy as np

from lie_learn.representations.SO3.irrep_bases import change_of_basis_matrix
from lie_learn.representations.SO3.pinchon_hoggan.pinchon_hoggan_dense import rot_mat, Jd
from lie_learn.representations.SO3.wigner_d import wigner_d_matrix, wigner_D_matrix
import lie_learn.spaces.S3 as S3

from .FFTBase import FFTBase
from scipy.fftpack import fft2, ifft2, fftshift

# TODO:
# Write testing code for these FFTs
# Write fast code for the real, quantum-normalized, centered / block-diagonal bases.
# The real Wigner-d functions d^l_mn are identically 0 whenever either (m < 0 and n >= 0) or (m >= 0 and n < 0),
#   so we can save work in the Wigner-d transform

class SO3_FT_Naive(FFTBase):
    """
    The most naive implementation of the discrete SO(3) Fourier transform:
    explicitly construct the Fourier matrix F and multiply by it to perform the Fourier transform.
    """

    def __init__(self, L_max, field='complex', normalization='quantum', order='centered', condon_shortley='cs'):

        super().__init__()

        # Explicitly construct the Wigner-D matrices evaluated at each point in a grid in SO(3)
        self.D = []
        b = L_max + 1
        for l in range(b):
            self.D.append(np.zeros((2 * b, 2 * b, 2 * b, 2 * l + 1, 2 * l + 1),
                                   dtype=complex if field == 'complex' else float))

            for j1 in range(2 * b):
                alpha = 2 * np.pi * j1 / (2. * b)
                for k in range(2 * b):
                    beta = np.pi * (2 * k + 1) / (4. * b)
                    for j2 in range(2 * b):
                        gamma = 2 * np.pi * j2 / (2. * b)
                        self.D[-1][j1, k, j2, :, :] = wigner_D_matrix(l, alpha, beta, gamma,
                                                                      field, normalization, order, condon_shortley)

        # Compute quadrature weights
        self.w = S3.quadrature_weights(b=b)

        # Convert D to a matrix that performs the FFT synthesis
        # The first axis corresponds to the spatial samples.
        # The spatial grid has shape (2b, 2b, 2b), so this axis has length (2b)^3
        # The second axis of this matrix has length sum_{l=0}^L_max (2l+1)^2,
        # which corresponds to all the spectral coefficients flattened into a vector.
        # (normally these are stored as matrices D^l of shape (2l+1)x(2l+1))
        self.F = np.hstack([self.D[l].reshape((2 * b) ** 3, (2 * l + 1) ** 2) for l in range(b)])

    def analyze(self, f):
        f_hat = []
        for l in range(f.shape[0] // 2):
            f_hat.append(np.einsum('ijkmn,ijk->mn', self.D[l], f * self.w[None, :, None]))
        return f_hat

    def analyze_by_matmul(self, f):
        f = f * self.w[None, :, None]
        f = f.flatten()
        return self.F.T.conj().dot(f)

    def synthesize(self, f_hat):
        b = len(self.D)
        f = np.zeros((2 * b, 2 * b, 2 * b), dtype=self.D[0].dtype)
        for l in range(b):
            f += np.einsum('ijkmn,mn->ijk', self.D[l], f_hat[l])
        return f

    def synthesize_by_matmul(self, f_hat):
        return self.F.dot(f_hat)


class SO3_FFT_SemiNaive_Complex(FFTBase):

    def __init__(self, L_max, d=None, w=None, L2_normalized=True,
                 field='complex', normalization='quantum', order='centered', condon_shortley='cs'):

        super().__init__()

        if d is None:
            self.d = setup_d_transform(
                b=L_max + 1, L2_normalized=L2_normalized,
                field=field, normalization=normalization,
                order=order, condon_shortley=condon_shortley)
        else:
            self.d = d

        if w is None:
            self.w = S3.quadrature_weights(b=L_max + 1)
        else:
            self.w = w

        self.wd = weigh_wigner_d(self.d, self.w)

    def analyze(self, f):
        return SO3_FFT_analyze(f, self.wd)

    def synthesize(self, f_hat):
        """
        Perform the inverse (spectral to spatial) SO(3) Fourier transform.

        :param f_hat: a list of matrices of with shapes [1x1, 3x3, 5x5, ..., 2 L_max + 1 x 2 L_max + 1]
        """
        return SO3_FFT_synthesize(f_hat, self.d)


class SO3_FFT_NaiveReal(FFTBase):

    def __init__(self, L_max, d=None, L2_normalized=True):

        self.L_max = L_max
        self.complex_fft = SO3_FFT_SemiNaive_Complex(L_max=L_max, d=d, L2_normalized=L2_normalized)

        # Compute change of basis function:
        self.c2b = [change_of_basis_matrix(l,
                                           frm=('complex', 'seismology', 'centered', 'cs'),
                                           to=('real', 'quantum', 'centered', 'cs'))
                    for l in range(L_max + 1)]

    def analyze(self, f):
        raise NotImplementedError('SO3 analyze function not implemented yet')

    def synthesize(self, f_hat):
        """
        """
        # Change basis on f_hat
        # We have R = B * C * B.conj().T, where
        # B is the real-to-complex change of basis, C are the complex Wigner D functions,
        # and R are the real Wigner D functions.
        # We want to compute Tr(eta^T R) = Tr( (B.T * eta * B.conj())^T C)
        f_hat_complex = [self.c2b[l].T.dot(f_hat[l]).dot(self.c2b[l].conj()) for l in range(self.L_max + 1)]

        f = self.complex_fft.synthesize(f_hat_complex)
        return f.real

    def synthesize_direct(self, f_hat):
        pass
        # Synthesize without using complex fft


def SO3_FFT_analyze(f, wd):
    """
    Compute the complex SO(3) Fourier transform of f.

    The standard way to define the FT is:
    \hat{f}^l_mn = (2 J + 1)/(8 pi^2) *
       int_0^2pi da int_0^pi db sin(b) int_0^2pi dc f(a,b,c) D^{l*}_mn(a,b,c)

    The normalizing constant comes about because:
    int_SO(3) D^*(g) D(g) dg = 8 pi^2 / (2 J + 1)
    where D is any Wigner D function D^l_mn. Note that the factor 8 pi^2 (the volume of SO(3))
    goes away if we integrate with the normalized Haar measure.

    This function computes the FT using the normalized D functions:
    \tilde{D} = 1/2pi sqrt((2J+1)/2) D
    where D are the rotation matrices in the basis of complex, seismology-normalized, centered spherical harmonics.

    Hence, this function computes:
    \hat{f}^l_mn = \int_SO(3) f(g) \tilde{D}^{l*}_mn(g) dg

    So that the FT of f = \tilde{D}^l_mn is 1 at (l,m,n) (and zero elsewhere).

    Args:
      f: an array of shape (2B, 2B, 2B), where B is the bandwidth.

    Returns:
      f_hat: the Fourier transform of f. A list of length B,
      where entry l contains an 2l+1 by 2l+1 array containing the projections
      of f onto matrix elements of the l-th irreducible representation of SO(3).

    Main source:
    SOFT: SO(3) Fourier Transforms
    Peter J. Kostelec and Daniel N. Rockmore

    Further information:
    Generalized FFTs-a survey of some recent results
    Maslen & Rockmore

    Engineering Applications of Noncommutative Harmonic Analysis.
    9.5 - Sampling and FFT for SO(3) and SU(2)
    G.S. Chrikjian, A.B. Kyatkin
    """
    assert f.shape[0] == f.shape[1]
    assert f.shape[1] == f.shape[2]
    assert f.shape[0] % 2 == 0

    # First, FFT along the alpha and gamma axes (axis 0 and 2, respectively)
    F = fft2(f, axes=(0, 2))
    F = fftshift(F, axes=(0, 2))

    # Then, perform the Wigner-d transform
    return wigner_d_transform_analysis(F, wd)

def SO3_FFT_synthesize(f_hat, d):
    """
    Perform the inverse (spectral to spatial) SO(3) Fourier transform.

    :param f_hat: a list of matrices of with shapes [1x1, 3x3, 5x5, ..., 2 L_max + 1 x 2 L_max + 1]
    """
    F = wigner_d_transform_synthesis(f_hat, d)

    # The rest of the SO(3) FFT is just a standard torus FFT
    F = fftshift(F, axes=(0, 2))
    f = ifft2(F, axes=(0, 2))

    b = len(d)
    return f * (2 * b) ** 2


def SO3_ifft(f_hat, d):
    """
    """
    b = len(d)
    df_hat = [d[l] * f_hat[l][:, None, :] for l in range(len(d))]

    # Note: the frequencies where m=-B or n=-B are set to zero,
    # because they are not used in the forward transform either
    # (the forward transform is up to m=-l, l<B
    F = np.zeros((2 * b, 2 * b, 2 * b), dtype=complex)
    for l in range(b):
        F[b - l:b + l + 1, :,  b - l:b + l + 1] += df_hat[l]

    F = fftshift(F, axes=(0, 2))
    f = ifft2(F, axes=(0, 2))
    return f * 2 * (b ** 2) / np.pi


def wigner_d_transform_analysis(f, wd):
    """
    The discrete Wigner-d transform [1] is defined as

    WdT(s)[l, m, n] = sum_k=^{2b-1} w_b(k) d^l_mn(beta_k) s_k

    where:
     - w_b(k) is the k-th quadrature weight for an order b grid,
     - d^l_mn is a Wigner-d function,
     - beta_k = pi(2k + 1) / 4b
     - s is a data vector of length 2b

    In practice we want to transform many data vectors at once; we have an input array of shape (

    [1] SOFT: SO(3) Fourier Transforms
    Peter J. Kostelec and Daniel N. Rockmore

    :param F:
    :param wd: the weighted Wigner-d functions, as returned by weigh_wigner_d()
    :return:
    """
    assert f.shape[0] == f.shape[1]
    assert f.shape[1] == f.shape[2]
    assert f.shape[0] % 2 == 0
    b = f.shape[0] // 2   # The bandwidth
    f0 = f.shape[0] // 2  # The index of the 0-frequency / DC component

    f_hat = []  # To store the result
    Z = 2 * np.pi / ((2 * b) ** 2)  # Normalizing constant

    # The array F we have computed so far still has shape (2b, 2b, 2b),
    # where the axes correspond to (M, beta, M').
    # For each l = 0, ..., b-1, select a subarray of shape (2l + 1, 2b, 2l + 1)
    f_sub = [f[f0 - l:f0 + l + 1, :, f0 - l:f0 + l + 1] for l in range(b)]

    for l in range(b):
        # Dot the vectors F_mn and d_mn over the middle axis (beta),
        # where -l <= m,n <= l, which corresponds to
        # f0 - l <= m,n < f0 + l + 1
        # for 0-based indexing and zero-frequency location f0
        f_hat.append(
            np.einsum('ijk,ijk->ik', wd[l], f_sub[l]) * Z
        )
    return f_hat


def wigner_d_transform_synthesis(f_hat, d):
    """
    The discrete Wigner-d transform [1] is defined as

    :param F:
    :param wd:
    :return:
    """
    b = len(d)  # bandwidth

    # Perform the brute-force Wigner-d transform
    # Note: the frequencies where m=-B or n=-B are set to zero,
    # because they are not used in the forward transform either
    # (the forward transform is up to m=-l, l<B
    df_hat = [d[l] * f_hat[l][:, None, :] for l in range(b)]
    F = np.zeros((2 * b, 2 * b, 2 * b), dtype=complex)
    for l in range(b):
        F[b - l:b + l + 1, :, b - l:b + l + 1] += df_hat[l]

    return F


def setup_d_transform(b, L2_normalized,
                      field='complex', normalization='quantum', order='centered', condon_shortley='cs'):
    """
    Precompute arrays of samples from the Wigner-d function, for use in the Wigner-d transform.

    Specifically, the samples that are required are:
    d^l_mn(beta_k)
    for:
     l = 0, ..., b - 1
     -l <= m, n <= l
     k = 0, ..., 2b - 1
     (where beta_k = pi (2 b + 1) / 4b)

    This data is returned as a list d indexed by l (of length b),
    where each element of the list is an array d[l] of shape (2l+1, 2b, 2l+1) indexed by (m, k, n)

    In the Wigner-d transform, for each l, we reduce an array d[l] of shape (2l+1, 2b, 2l+1)
     against a data array of the same shape, along the beta axis (axis 1 of length 2b).

    :param b: bandwidth of the transform
    :param L2_normalized: whether to use L2_normalized versions of the Wigner-d functions.
    :param field, normalization, order, condon_shortley: the basis and normalization convention (see irrep_bases.py)
    :return a list d of length b, where d[l] is an array of shape (2l+1, 2b, 2l+1)
    """
    # Compute array of beta values as described in SOFT 2.0 documentation
    beta = np.pi * (2 * np.arange(0, 2 * b) + 1) / (4. * b)

    # For each l=0, ..., b-1, we compute a 3D tensor of shape (2l+1, 2b, 2l+1) for axes (m, beta, n)
    # Together, these indices (l, m, beta, n) identify d^l_mn(beta)
    convention = {'field': field, 'normalization': normalization, 'order': order, 'condon_shortley': condon_shortley}
    d = [np.array([wigner_d_matrix(l, bt, **convention) for bt in beta]).transpose(1, 0, 2)
         for l in range(b)]

    if L2_normalized:  # TODO: this should be integrated in the normalization spec above, no?
        # The Unitary matrix elements have norm:
        # | U^\lambda_mn |^2 = 1/(2l+1)
        # where the 2-norm is defined in terms of normalized Haar measure. [Haar measure on SO(3) or SO(2)? SO3 probs]
        # So T = sqrt(2l + 1) U are L2-normalized functions
        d = [d[l] * np.sqrt(2 * l + 1) for l in range(len(d))]

        # We want the L2 normalized functions:
        # d = [d[l] * np.sqrt(l + 0.5) for l in range(len(d))]

    return d


def weigh_wigner_d(d, w):
    """
    The Wigner-d transform involves a sum where each term is a product of data, d-function, and quadrature weight.
     Since the d-functions and quadrature weights don't depend on the data, we can precompute their product.
    We have a quadrature weight for each value of beta and beta corresponds to the second axis of d,
    so the weights are broadcast over the other axes.

    :param d: a list of samples of the Wigner-d function, as returned by setup_d_transform
    :param w: an array of quadrature weights, as returned by S3.quadrature_weights
    :return: the weighted d function samples, with the same shape as d
    """
    return [d[l] * w[None, :, None] for l in range(len(d))]


def SO3_convolve(f, g, dw=None, d=None):

    assert f.shape == g.shape
    assert f.shape[0] % 2 == 0
    b = f.shape[0] / 2

    if d is None:
        d = setup_d_transform(b)

    # To convolve, first perform a Fourier transform on f and g:
    F = SO3_fft(f, dw)
    G = SO3_fft(g, dw)

    # The Fourier transform of the convolution f*g is the matrix product FG
    # of their Fourier transforms F and G:
    FG = [np.dot(a, b) for (a, b) in zip(F, G)]

    # The convolution is obtain by inverse Fourier transforming FG:
    return SO3_ifft(FG, d)


def SO3_convolve_complex_fft(f, g):
    assert f.shape == g.shape
    assert f.shape[0] % 2 == 0
    b = f.shape[0] / 2

    fft = SO3_FFT_SemiNaive_Complex(L_max=b - 1, d=None, w=None, L2_normalized=False)

    f_hat = fft.analyze(f)
    g_hat = fft.analyze(g)

    fg_hat = [np.dot(a, b) for (a, b) in zip(f_hat, g_hat)]

    return fft.synthesize(fg_hat)