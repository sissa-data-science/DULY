# Copyright 2021-2024 The DADApy Authors. All Rights Reserved.
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
# ==============================================================================

"""
The *density_advanced* module contains the *DensityEstimation* class.

Different algorithms to estimate the logdensity, the logdensity gradientest and the logdensity differences are
implemented as methods of this class. In particular, differently from the methods implemented in the DensityEstimation,
the methods in the DensityEstimation class are based on the sparse neighbourhood graph structure which is implemented
in the NeighGraph class.
"""

import multiprocessing
import time
import warnings

import numpy as np
from scipy import linalg as slin
from scipy import sparse

from dadapy._cython import cython_grads as cgr
from dadapy.neigh_graph import NeighGraph
from dadapy.density_estimation import DensityEstimation

cores = multiprocessing.cpu_count()


class DensityAdvanced(DensityEstimation, NeighGraph):
    """Computes the log-density and (where implemented) its error at each point and other properties.

    Inherits from class NeighGraph.
    AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
    Can return an estimate of the gradient of the log-density at each point and an estimate of the error on each
    component using an improved version of the mean-shift gradient algorithm [Fukunaga1975][Carli2023]
    Can return an estimate of log-density differences and their error each point based on the gradient estimates.
    Can compute the log-density and its error at each using BMTI.


    Attributes:
        grads (np.ndarray(float), optional): the gradient components estimated from from each point i
        grads_var (np.ndarray(float), optional): for each line i contains the estimated variance of the gradient
            components at point i
        check_grads_covmat (bool, optional): it is flagged "True" when grads_var contains the variance-covariance
            matrices of the gradients
        Fij_array (list(np.array(float)), optional): stores for each couple in nind_list the estimates of deltaF_ij
            computed from point i as semisum of the gradients in i and minus the gradient in j
        Fij_var_array (np.array(float), optional): stores for each couple in nind_list the estimates of the squared
            errors on the values in Fij_array
        inv_deltaFs_cov

    """

    def __init__(
        self, coordinates=None, distances=None, maxk=None, verbose=False, n_jobs=cores
    ):
        """Initialise the DensityEstimation class."""
        super().__init__(
            coordinates=coordinates,
            distances=distances,
            maxk=maxk,
            verbose=verbose,
            n_jobs=n_jobs,
        )

        self.grads = None
        self.grads_var = None
        self.grads_covmat = None
        self.check_grads_covmat = False
        self.Fij_array = None
        self.Fij_var_array = None
        self.Fij_var_array = None
        self.inv_deltaFs_cov = None

    # ----------------------------------------------------------------------------------------------

    def compute_grads(self, comp_covmat=False):
        """Compute the gradient of the log density each point using k* nearest neighbors.
        The gradient is estimated via a linear expansion of the density propagated to the log-density.

        Args:

        Returns:

        MODIFICARE QUI E ANCHE NEGLI ATTRIBUTI

        """
        # compute optimal k
        if self.kstar is None:
            self.compute_kstar()

        # check or compute vector_diffs
        if self.neigh_vector_diffs is None:
            self.compute_neigh_vector_diffs()

        if self.verb:
            print("Estimation of the density gradient started")

        sec = time.time()
        if comp_covmat is False:
            self.grads, self.grads_var = cgr.return_grads_and_var_from_nnvecdiffs(
                self.neigh_vector_diffs,
                self.nind_list,
                self.nind_iptr,
                self.kstar,
                self.intrinsic_dim,
            )
            self.grads_var = np.einsum(
                "ij, i -> ij", self.grads_var, self.kstar / (self.kstar - 1)
            )  # Bessel's correction for the unbiased sample variance estimator

        else:
            self.grads, self.grads_covmat = cgr.return_grads_and_covmat_from_nnvecdiffs(
                self.neigh_vector_diffs,
                self.nind_list,
                self.nind_iptr,
                self.kstar,
                self.intrinsic_dim,
            )

            # Bessel's correction for the unbiased sample variance estimator
            self.grads_covmat = np.einsum(
                "ijk, i -> ijk", self.grads_covmat, self.kstar / (self.kstar - 1)
            )

            # get diagonal elements of the covariance matrix
            self.grads_var = np.zeros((self.N, self.dims))
            for i in range(self.N):
                self.grads_var[i, :] = np.diag(self.grads_covmat[i, :, :])

        sec2 = time.time()
        if self.verb:
            print("{0:0.2f} seconds computing gradients".format(sec2 - sec))

    # ----------------------------------------------------------------------------------------------

    def compute_deltaFs(self, pearson_method="jaccard", comp_p_mat=False):
        """Compute deviations deltaFij to standard kNN log-densities at point j as seen from point i using
            a linear expansion with as slope the semisum of the average gradient of the log-density over
            the neighbourhood of points i and j. The parameter chi is used in the estimation of the squared error of
            the deltaFij as 1/4*(E_i^2+E_j^2+2*E_i*E_j*chi), where E_i is the error on the estimate of grad_i*DeltaX_ij.

        Args:
            pearson_method: the Pearson correlation coefficient between the estimates of the gradient in i and j.
                Can take a numerical value between 0 and 1. The option 'auto' takes a geometrical estimate of chi based
                on AAAAAAAAA

        Returns:

        """

        if self.grads_covmat is None:
            self.compute_grads(comp_covmat=True)

        if self.verb:
            print(
                "Estimation of the gradient semisum (linear) corrections deltaFij to the log-density started"
            )
        sec = time.time()

        Fij_array = np.zeros(self.nspar)
        self.Fij_var_array = np.zeros(self.nspar)

        g1 = self.grads[self.nind_list[:, 0]]
        g2 = self.grads[self.nind_list[:, 1]]
        g_var1 = self.grads_covmat[self.nind_list[:, 0]]
        g_var2 = self.grads_covmat[self.nind_list[:, 1]]

        # check or compute common_neighs
        if self.pearson_mat is None:
            self.compute_pearson(method=pearson_method, comp_p_mat=comp_p_mat)

        Fij_array = 0.5 * np.einsum("ij, ij -> i", g1 + g2, self.neigh_vector_diffs)
        vari = np.einsum(
            "ij, ij -> i",
            self.neigh_vector_diffs,
            np.einsum("ijk, ik -> ij", g_var1, self.neigh_vector_diffs),
        )
        varj = np.einsum(
            "ij, ij -> i",
            self.neigh_vector_diffs,
            np.einsum("ijk, ik -> ij", g_var2, self.neigh_vector_diffs),
        )
        self.Fij_var_array = 0.25 * (
            vari + varj + 2 * self.pearson_array * np.sqrt(vari * varj)
        )

        sec2 = time.time()
        if self.verb:
            print("{0:0.2f} seconds computing gradient corrections".format(sec2 - sec))

        self.Fij_array = Fij_array
        self.Fij_var_array = self.Fij_var_array
        # self.Fij_var_array = self.Fij_var_array*k1/(k1-1) #Bessel's correction?

    # ----------------------------------------------------------------------------------------------

    def compute_deltaFs_inv_cross_covariance(self, pearson_method="jaccard"):
        """Compute the cross-covariance of the deltaFs cov[deltaFij,deltaFlm] using cython.
            AAAAAAAAAAAAAAAA possibile spostarlo in utils al momento.
            Peraltro qui bisogna trovare un modo per farlo funzionare

        Args: AAAAAAAAAAAAAAAAA

        Returns: AAAAAAAAAAAAAAAAA

        """

        # check for deltaFs
        if self.pearson_mat is None:
            self.compute_pearson(method=pearson_method, comp_p_mat=True)

        # check or compute deltaFs_grads_semisum
        if self.Fij_var_array is None:
            self.compute_deltaFs()
        # AAAAAAAAAAAAAAA controllare se serve
        # smallnumber = 1.e-10
        # data.grads_var += smallnumber*np.tile(np.eye(data.dims),(data.N,1,1))
        # AAAAAAAAAAAAAAA fine controllare se serve

        if self.verb:
            print("Estimation of the deltaFs cross-covariance started")
        sec = time.time()
        # compute a diagonal approximation of the inverse of the cross-covariance matrix
        self.inv_deltaFs_cov = cgr.return_deltaFs_inv_cross_covariance(
            self.grads_var,
            self.neigh_vector_diffs,
            self.nind_list,
            self.pearson_mat,
            self.Fij_var_array,
        )

        sec2 = time.time()
        if self.verb:
            print(
                "{0:0.2f} seconds computing the deltaFs cross-covariance".format(
                    sec2 - sec
                )
            )

    # ----------------------------------------------------------------------------------------------

    def compute_density_BMTI(
        self,
        delta_F_err="uncorr",
        comp_log_den_err=False,
        mem_efficient=False,
    ):
        # inv_cov_method    = uncorr assumes the cross-covariance matrix is diagonal with diagonal = Fij_var_array;
        #           = LSDI (Least Squares with respect to a Diagonal Inverse) inverts the cross-covariance C
        #             by finding the approximate diagonal inverse which multiplied by C gives the least-squared
        #             closest matrix to the identity in the Frobenius norm
        # use_variance  = True uses the elements of the inverse cross-covariance to define the A matrix;
        #               = False assumes the cross-covraiance matix is equal to the nspar x nspar identity
        # redundancy_factor (used only if method=uncorr)
        # comp_err
        # mem_efficient = True uses sparse matrices;
        #               = False uses dense NxN matrices

        # call compute_density_BMTI_reg with alpha=1 and log_den and log_den_err as arrays of ones
        self.compute_density_BMTI_reg(
            alpha=1.0,
            log_den=np.ones(self.N),
            log_den_err=np.ones(self.N),
            delta_F_err=delta_F_err,
            comp_log_den_err=comp_log_den_err,
            mem_efficient=mem_efficient,
        )

    # ----------------------------------------------------------------------------------------------
    # ----------------------------------------------------------------------------------------------

    def compute_density_BMTI_reg(
        self,
        alpha=0.1,
        log_den=None,
        log_den_err=None,
        delta_F_err="uncorr",
        comp_log_den_err=False,
        mem_efficient=False,
    ):
        # compute changes in free energy
        if self.Fij_array is None:
            self.compute_deltaFs()

        # note: this should be called after the computation of the deltaFs
        # since otherwhise self.log_den and self.log_den_err are redefined to None via set kstar
        if log_den is not None and log_den_err is not None:
            self.log_den = log_den
            self.log_den_err = log_den_err
        else:
            self.compute_density_kstarNN()

        # add a warnings.warning if self.N > 10000 and mem_efficient is False
        if self.N > 15000 and mem_efficient is False:
            warnings.warn(
                "The number of points is large and the memory efficient option is not selected. \
                If you run into memory issues, consider using the slower memory efficient option."
            )

        if self.verb:
            print("BMTI density estimation started")
            sec = time.time()

        # define the likelihood covarince matrix
        A, deltaFcum = self._get_BMTI_reg_linear_system(delta_F_err, alpha)

        sec2 = time.time()

        if self.verb:
            print("{0:0.2f} seconds to fill sparse matrix".format(sec2 - sec))

        # solve linear system
        log_den = self._solve_BMTI_reg_linar_system(A, deltaFcum, mem_efficient)
        self.log_den = log_den

        if self.verb:
            print("{0:0.2f} seconds to solve linear system".format(time.time() - sec2))
        sec2 = time.time()

        # compute error
        if comp_log_den_err is True:
            A = A.todense()
            B = slin.pinvh(A)
            self.log_den_err = np.sqrt(np.diag(B))

            if self.verb:
                print("{0:0.2f} seconds inverting A matrix".format(time.time() - sec2))

            sec2 = time.time()

        # self.log_den_err = np.sqrt(np.diag(slin.pinvh(A.todense())))
        # self.log_den_err = np.sqrt(diag/np.array(np.sum(np.square(A.todense()),axis=1)).reshape(self.N,))

        sec2 = time.time()
        if self.verb:
            print("{0:0.2f} seconds for BMTI density estimation".format(sec2 - sec))

    # ----------------------------------------------------------------------------------------------

    def _get_BMTI_reg_linear_system(self, delta_F_err, alpha):
        if delta_F_err == "uncorr":
            # define redundancy factor for each A matrix entry as the geometric mean of the 2 corresponding k*
            k1 = self.kstar[self.nind_list[:, 0]]
            k2 = self.kstar[self.nind_list[:, 1]]
            redundancy = np.sqrt(k1 * k2)

            tmpvec = (
                np.ones(self.nspar, dtype=np.float_) / self.Fij_var_array / redundancy
            )
        elif delta_F_err == "LSDI":
            self.compute_deltaFs_inv_cross_covariance()
            tmpvec = self.inv_deltaFs_cov

        elif delta_F_err == "none":
            tmpvec = np.ones(self.nspar, dtype=np.float_)

        else:
            raise ValueError(
                "The delta_F_err parameter is not valid, choose 'uncorr', 'LSDI' or 'none'"
            )

        # compute adjacency matrix
        A = sparse.csr_matrix(
            (-tmpvec, (self.nind_list[:, 0], self.nind_list[:, 1])),
            shape=(self.N, self.N),
            dtype=np.float_,
        )

        # compute coefficients vector
        supp_deltaF = sparse.csr_matrix(
            (self.Fij_array * tmpvec, (self.nind_list[:, 0], self.nind_list[:, 1])),
            shape=(self.N, self.N),
            dtype=np.float_,
        )

        # make A symmetric
        A = alpha * sparse.lil_matrix(A + A.transpose())

        # insert kstarNN with factor 1-alpha in the Gaussian approximation
        # ALREADY MULTIPLIED A BY ALPHA
        diag = (
            np.array(-A.sum(axis=1)).reshape((self.N,))
            + (1.0 - alpha) / self.log_den_err**2
        )

        A.setdiag(diag)

        deltaFcum = (
            alpha
            * (
                np.array(supp_deltaF.sum(axis=0)).reshape((self.N,))
                - np.array(supp_deltaF.sum(axis=1)).reshape((self.N,))
            )
            + (1.0 - alpha) * self.log_den / self.log_den_err**2
        )

        return A, deltaFcum

    def _solve_BMTI_reg_linar_system(self, A, deltaFcum, mem_efficient):
        if mem_efficient is False:
            log_den = np.linalg.solve(A.todense(), deltaFcum)
        else:
            log_den = sparse.linalg.spsolve(A.tocsr(), deltaFcum)

        return log_den
