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
The *diff_imbalance* module contains the *DiffImbalance* class, implemented with JAX.

The only method supposed to be called by the user is 'train', which carries out the automatic optimization ot the 
Differential Information as a function of the weights of the features in the first distance space. 
The code can be runned on gpu using the command
    jax.config.update('jax_platform_name', 'gpu') # 'cpu' or 'gpu
"""

import jax
import jax.numpy as jnp
from tqdm.auto import tqdm
from flax.training import train_state
import optax
import warnings

# OPTIMIZABLE DISTANCE FUNCTIONS
# ----------------------------------------------------------------------------------------------


# for feature selection
@jax.jit
def _compute_dist2_matrix_scaling(params, batch_rows, batch_columns, periods=None):
    """Computes the (squared) Euclidean distance matrix between points in 'batch_rows' and points in 'batch_columns'.

    The features of the points are scaled by the weights in 'params', such that the distance between
    point i in batch_rows and point j in batch_columns is computed as
        dist2_matrix[i,j] = ((batch_rows[i,:] - batch_columns[j,:])**2).sum(axis=-1)

    Args:
        params (jnp.array(float)): array of shape (n_features,)
        batch_rows (jnp.array(float)): matrix of shape (n_points_rows, n_features)
        batch_columns (jnp.array(float)): matrix of shape (n_points_columns, n_features)
        periods (jnp.array(float)): array of shape (n_features,) for computing distances between periodic
            features by applying PBCs. If only a subset of features is periodic, the 'periods' entries for the
            remaining features should be set to zero.

    Returns:
        dist2_matrix (jnp.array(float)): array of shape (n_points_rows, n_features) containing the square Euclidean
            distances between all points in 'batch_rows' and all points in 'batch_columns'
    """
    diffs = batch_rows[:, jnp.newaxis, :] - batch_columns[jnp.newaxis, :, :]
    if periods is not None:  # nonperiodic features must have entry '0'
        diffs -= jnp.where(periods, 1.0, 0.0) * jnp.round(diffs / periods) * periods
    diffs *= params[jnp.newaxis, jnp.newaxis, :]
    dist2_matrix = jnp.sum(diffs * diffs, axis=-1)
    return dist2_matrix


# CLASS TO OPTIMIZE DIFFERENTIAL IMBALANCE
# ----------------------------------------------------------------------------------------------


class DiffImbalance:
    """Carries out the optimization of the DII(A(w)->B) with respect to the weights in the first distance space.

    The class 'DiffImbalance' supports three schemes for setting the smoothing parameter lambda, which tunes the
    size of neighborhoods in space A. In all the schemes lambda can be epoch-dependent, i.e. decreased during the
    training according to a cosine decay between 'init' and 'final' values. The schemes are:

        1. Global and non-adaptive: lambda is equal for all the points and its value is explicitely set by the user.

        Example:
            point_adapt_lambda: False
            k_init: null
            k_final: null
            lambda_init: 1
            lambda_final: 1e-2

        2. Global and adaptive: lambda is equal for all the points and is set as a fraction (1/10) of the average square distance of neighbor k.

        Example:
            point_adapt_lambda: False
            k_init: 10
            k_final: 1
            lambda_init: None  # lambda_init and lambda_final are ignored if k_init and k_final are not None
            lambda_final: None

        3. Point-dependent and adaptive: lambda is different for each point and is set as a fraction (1/10) of the square distance of neighbor k.

        Example:
            point_adapt_lambda: True
            k_init: 10
            k_final: 1
            lambda_init: None # lambda_init and lambda_final are ignored if k_init and k_final are not None
            lambda_final: None

    Attributes:
        data_A (np.array(float), jnp.array(float)): feature space A, matrix of shape (n_points, n_features_A)
        data_B (np.array(float), jnp.array(float)): feature space B, matrix of shape (n_points, n_features_B)
        periods_A (np.array(float), jnp.array(float)): array of shape (n_features_A,), periods of features A.
            The default is None, which means that the features A are treated as nonperiodic.
        periods_B (np.array(float), jnp.array(float)): array of shape (n_features_B,), periods of features B.
            The default is None, which means that the features B are trated as nonperiodic.
        seed (int): seed of jax random generator
        num_epochs (int): number of training epochs
        batches_per_epoch (int): number of minibatches; must be a divisor of n_points. Each update of the weights is
            carried out by computing the gradient over n_points / batches_per_epoch points. The default is 1, which
            means that the gradient is computed over all the available points (batch GD).
        l1_strength (float): strength of the L1 regularization (LASSO) term. The default is 0.
        point_adapt_lambda (bool): whether to use a global smoothing parameter lambda for the c_ij coefficients
            in the DII (if False), or a different parameter for each point (if True). The default is False.
        k_init (int): initial rank of the neighbors used to set lambda. The default is 1.
        k_final (int): initial rank of the neighbors used to set lambda. The default is 1.
        lambda_init (float): initial value of lambda
        lambda_final (float): final value of lambda
        init_params (np.array(float), jnp.array(float)): array of shape (n_features_A,) containing the initial
            values of the scaling weights to be optimized. If None, init_params == [0.1, 0.1, ..., 0.1].
        optimizer_name (str): name of the optimizer, calling the Optax library. The possible choices are 'sgd'
            (default), 'adam' and 'adamw'. See https://optax.readthedocs.io/en/latest/api/optimizers.html for
            more.
        learning_rate (float): value of the learning rate. The default is 1e-1.
        learning_rate_decay (bool): whether to damp the learning rate to zero following a cosine decay schedule,
            if True, or keep it to a constant value, if False. The default is True.
        compute_error (bool): whether to compute the standard Information Imbalance, if False (default), or to
            compute distances between points in two different groups and return the error associated to the DII
            during the training, if True
        ratio_rows_columns (float): only read when compute_error == True, defines the ratio between the number
            of points along the rows and the number points along the columns of distance and rank matrices, in two
            groups randomly sampled. The default is 1, which means that the two groups are constructed with
            n_points / 2 and n_points / 2 points.
        num_points_rows (int): number of points sampled from the rows of rank and distance matrices. In case of large
            datasets, choosing num_points_rows < n_points can significantly speed up the training. The default is
            None, for which num_points_rows == n_points.
    """

    def __init__(
        self,
        data_A,
        data_B,
        periods_A=None,
        periods_B=None,
        seed=0,
        num_epochs=100,
        batches_per_epoch=1,
        l1_strength=0.0,
        point_adapt_lambda=False,
        k_init=1,
        k_final=1,
        lambda_init=None,
        lambda_final=None,
        init_params=None,
        optimizer_name="sgd",
        learning_rate=1e-1,
        learning_rate_decay=True,
        compute_error=False,
        ratio_rows_columns=1,
        num_points_rows=None
    ):
        """Initialise the DiffImbalance class."""
        self.nfeatures_A = data_A.shape[1]
        self.nfeatures_B = data_B.shape[1]
        assert data_A.shape[0] == data_B.shape[0], (
            f"Error: space A has {data_A.shape[0]} samples "
            + f"while space B has {data_B.shape[0]} samples."
        )
        # initialize jax random generator
        self.key = jax.random.PRNGKey(seed)
        self.key, subkey = jax.random.split(self.key, num=2)

        # initialize spaces A and B
        self.data_A = data_A
        self.data_B = data_B

        if compute_error:
            assert num_points_rows is None, (
                f"Error: if compute_error == True, the argument num_points_rows cannot be set."
            )
            nrows = int(0.5 * ratio_rows_columns * data_A.shape[0])
            indices_rows = jax.random.choice(
                self.key, jnp.arange(data_A.shape[0]), shape=(nrows,), replace=False
            )
            indices_columns = jnp.delete(jnp.arange(data_A.shape[0]), indices_rows)
            self.max_rank = indices_columns.shape[0]  # for correct normalization
        elif num_points_rows is not None:
            # decimate rows but not columns, and keep same indices in upper left square matrix
            indices_rows = jax.random.choice(
                self.key, jnp.arange(data_A.shape[0]), shape=(num_points_rows,), replace=False
            )
            indices_columns = jnp.delete(jnp.arange(data_A.shape[0]), indices_rows)
            indices_columns = jnp.concatenate((indices_rows, indices_columns)) 
            self.max_rank = indices_columns.shape[0] - 1  # for correct normalization
        else:
            indices_rows = jnp.arange(data_A.shape[0])
            indices_columns = +indices_rows
            self.max_rank = indices_columns.shape[0] - 1  # for correct normalization
        self.data_A_rows = data_A[indices_rows]
        self.data_A_columns = data_A[indices_columns]
        self.data_B_rows = data_B[indices_rows]
        self.data_B_columns = data_B[indices_columns]

        self.nrows = self.data_A_rows.shape[0]
        self.periods_A = (
            jnp.ones(self.nfeatures_A) * jnp.array(periods_A)
            if periods_A is not None
            else periods_A
        )
        self.periods_B = (
            jnp.ones(self.nfeatures_B) * jnp.array(periods_B)
            if periods_B is not None
            else periods_B
        )
        self.num_epochs = num_epochs
        self.batches_per_epoch = batches_per_epoch
        self.l1_strength = l1_strength
        self.k_init = k_init
        self.k_final = k_final

        # for efficiency reasons, faster sort in adaptive-lambda scheme
        self.k_max_allowed = 100 if self.max_rank > 100 else self.max_rank + 1

        self.lambda_init = lambda_init
        self.lambda_final = lambda_final
        if init_params is not None:
            self.init_params = init_params
        else:
            self.init_params = 0.1 * jnp.ones(self.nfeatures_A)
        self.final_params = None
        self.optimizer_name = optimizer_name
        self.learning_rate = learning_rate
        self.learning_rate_decay = learning_rate_decay
        self.compute_error = compute_error
        self.state = None
        self._distance_A = _compute_dist2_matrix_scaling  # TODO: assign other functions if other distances A are chosen

        # generic checks and warnings
        assert self.nrows >= batches_per_epoch, (
            f"Error: cannot extract {batches_per_epoch} minibatches "
            + f"from {self.nrows} samples."
        )
        if point_adapt_lambda:
            assert self.k_init is not None and self.k_final is not None, (
                f"Error: provide values of 'k_init' and 'k_final' "
                + f"to compute lambda in a point-adaptive fashion."
            )
        if self.k_init is not None:
            if self.k_init > 100:
                warnings.warn(
                    f"For efficiency reasons the maximum value for 'k_init' is 100, while you set it to {self.k_init}.\n"
                    + f"The run will continue with 'k_init = 100'"
                )
                self.k_init = 100
            assert (
                self.k_init >= self.k_final
            ), f"Error: 'k_init' ({self.k_init}) cannot be smaller than 'k_final' ({self.k_final})"
        if self.lambda_init is not None:
            assert (
                self.lambda_init >= self.lambda_final
            ), f"Error: 'k_init' ({self.lambda_init}) cannot be smaller than 'k_final' ({self.lambda_final})"

        # create jitted functions
        self._create_functions()
        self.ranks_B = self._compute_rank_matrix_B(
            batch_rows=self.data_B_rows,
            batch_columns=self.data_B_columns,
            periods=self.periods_B,
        )

        # set method to compute lambda (adaptive and point-dependent, adaptive and global, non-adaptive and global)
        if point_adapt_lambda:
            self.lambda_method = self._compute_point_adapt_lambdas
        elif self.k_init is not None and self.k_final is not None:
            self.lambda_method = self._compute_adapt_lambda
        elif self.lambda_init is not None and self.lambda_final is not None:
            self.lambda_method = self._compute_lambda_decay

    def _create_functions(self):
        def _compute_rank_matrix_B(batch_rows, batch_columns, periods):
            """Compute the matrix of ranks for the target space B.

            Args:
                batch_rows (jnp.array(float)): matrix of shape (n_points_rows, n_features_B), containing
                    the points labelling the rows of the rank matrix
                batch_columns (jnp.array(float)): matrix of shape (n_points_columns, n_features_B), containing
                    the points labelling the columns of the rank matrix
                periods (jnp.array(float)): array of shape (n_features_B,), containing the periods of each feature
                    in space B. PBCs are not applied for feature i if periods[i] == 0, or if periods = None.

            Returns:
                rank_matrix (jnp.array(float)): matrix of shape (n_points_rows, n_points_columns), defining the
                    target distance ranks in space B. Ranks start from 1, and are 0 only for a point with respect
                    to itself.
            """
            diffs = batch_rows[:, jnp.newaxis, :] - batch_columns[jnp.newaxis, :, :]
            if periods is not None:  # nonperiodic features must have entry '0'
                diffs -= (
                    jnp.where(periods, 1.0, 0.0) * jnp.round(diffs / periods) * periods
                )
            dist2_matrix = jnp.sum(diffs * diffs, axis=-1)
            rank_matrix = dist2_matrix.argsort(axis=1).argsort(axis=1)
            if self.compute_error:
                rank_matrix = rank_matrix + 1
            return rank_matrix

        def _cosine_decay_func(start_value, final_value, step):
            """Carry out a cosine decay during the training.

            The arguments start_value and final_value can be values of the learning rate, of the smoothing parameter
            lambda or of the neighbor order used to compute lambda in the adaptive scheme.

            Args:
                start_value (float): initial value
                final_value (jnp.array(float)): final value
                step (int): number of current gradient descent step

            Returns:
                cosine (float): value of the cosine interpolating start_value at step 0 and final_value at the last
                training step
            """
            x = jnp.pi / (self.num_epochs * self.batches_per_epoch) * step
            cosine = (start_value - final_value) * (jnp.cos(x) + 1) / 2 + final_value
            return cosine

        def _compute_point_adapt_lambdas(dist2_matrix, step):
            """Compute lambda parameters with the point-adaptive scheme, according to the current value of k.

            Args:
                dist2_matrix (jnp.array(float)): matrix of shape (n_points_rows, n_points_columns), containing
                    the squared distances in space A at the current training step
                step (int): number of current gradient descent step

            Returns:
                current_lambdas (jnp.array(float)): array of shape (n_points_rows,), containing a value of lambda
                    computed adaptively for each point, as the fraction (1/10) of the squared distance of the
                    neighbor of order k
            """
            current_k = jnp.rint(
                _cosine_decay_func(
                    start_value=self.k_init, final_value=self.k_final, step=step
                )
            ).astype(int)
            # take the k_max_allowed smallest distances with negative sign
            smallest_dist2, _ = jax.lax.top_k(-dist2_matrix, self.k_max_allowed)
            current_lambdas = -smallest_dist2[:, current_k] * 0.1
            return current_lambdas

        def _compute_adapt_lambda(dist2_matrix, step):
            """Compute lambda parameter with the global and adaptive scheme, according to the current value of k.

            Args:
                dist2_matrix (jnp.array(float)): matrix of shape (n_points_rows, n_points_columns), containing
                    the squared distances in space A at the current training step
                step (int): number of current gradient descent step

            Returns:
                current_lambda (jnp.array(float)): array of shape (n_points_rows,), containing the same value of lambda
                    for all points, computed as the fraction (1/10) of the average squared distance of the neighbor
                    of order k
            """
            current_lambda = _compute_point_adapt_lambdas(
                dist2_matrix, step
            ).mean() * jnp.ones(dist2_matrix.shape[0])
            return current_lambda

        def _compute_lambda_decay(dist2_matrix, step):
            """Compute global and non-adaptive lambda, according to user's inputs (lambda_init and lambda_final)

            Args:
                dist2_matrix (jnp.array(float)): matrix of shape (n_points_rows, n_points_columns), containing
                    the squared distances in space A at the current training step
                step (int): number of current gradient descent step

            Returns:
                current_lambda (jnp.array(float)): array of shape (n_points_rows,), containing the same value of lambda
                    for all points, computed with a cosine decay between values lambda_init and lambda_final
            """
            current_lambda = _cosine_decay_func(
                start_value=self.lambda_init, final_value=self.lambda_final, step=step
            ) * jnp.ones(dist2_matrix.shape[0])
            return current_lambda

        def _compute_diff_imbalance(
            params, batch_A_rows, batch_A_columns, batch_B_ranks, step, batch_indices
        ):
            """Compute the Differentiable Information Imbalance (DII) at the current step of the training.

            Args:
                params (jnp.array(float)): array of shape (n_features_A,) of the current feature weights
                batch_A_rows (jnp.array(float)): matrix of shape (n_points_rows, n_features_A), containing
                    the points labelling the rows of the distance matrix
                batch_A_columns (jnp.array(float)): matrix of shape (n_points_columns, n_features_A), containing
                    the points labelling the columns of the distance matrix
                batch_B_ranks (jnp.array(float)): matrix of shape (n_points_rows, n_points_columns), containing
                    the pre-computed target ranks in space B
                step (int): number of current gradient descent step
                batch_indices: indices of points included in the current mini-batch

            Returns:
                diff_imbalance (float): current value of the DII
                error_imbalance (float): error associated to the current value of the DII, only used when
                    return_error == True but always returned for compatibility
            """
            dist2_matrix_A = self._distance_A(  # compute distance matrix A
                params=params,
                batch_rows=batch_A_rows,
                batch_columns=batch_A_columns,
                periods=self.periods_A,
            )
            N = dist2_matrix_A.shape[0]
            if (
                not self.compute_error
            ):  # set distance of a point with itself to large number
                dist2_matrix_A = dist2_matrix_A.at[jnp.arange(N), batch_indices].set(
                    +1e10
                )
            lambdas = self.lambda_method(  # compute lambda values
                dist2_matrix=dist2_matrix_A, step=step
            )
            c_matrix = jax.nn.softmax(  # NB. diagonale elements are zero if not self.compute_error
                -dist2_matrix_A / lambdas[:, jnp.newaxis], axis=1
            )

            # compute DII and its error
            conditional_ranks = jnp.sum(batch_B_ranks * c_matrix, axis=1)
            diff_imbalance = 2.0 / (self.max_rank + 1) * jnp.sum(conditional_ranks) / N
            error_imbalance = (
                2.0
                / (self.max_rank + 1)
                * jnp.std(conditional_ranks, ddof=1)
                / jnp.sqrt(N)
            )
            return diff_imbalance, error_imbalance

        def _train_step(
            state, batch_A_rows, batch_A_columns, batch_B_ranks, batch_indices
        ):
            """Perform a single gradient descent step in the optimization of the DII.

            Args:
                state (flax.training.train_state.TrainState object): current training state
                batch_A_rows (jnp.array(float)): matrix of shape (n_points_rows, n_features_A), containing
                    the points labelling the rows of the distance matrix
                batch_A_columns (jnp.array(float)): matrix of shape (n_points_columns, n_features_A), containing
                    the points labelling the columns of the distance matrix
                batch_B_ranks (jnp.array(float)): matrix of shape (n_points_rows, n_points_columns), containing
                    the pre-computed target ranks in space B
                batch_indices: indices of points included in the current mini-batch

            Returns:
                state_new (flax.training.train_state.TrainState object): new training state after optimizer step
                imb (flat): new value of the DII after optimizer step
                error (float): error associated to the current value of the DII, only used when return_error == True
                    but always returned for compatibility
            """
            loss_fn = lambda params: _compute_diff_imbalance(
                params=params,
                batch_A_rows=batch_A_rows,
                batch_A_columns=batch_A_columns,
                batch_B_ranks=batch_B_ranks,
                step=state.step,
                batch_indices=batch_indices,
            )
            # Get loss and gradient
            imb_and_error, grads = jax.value_and_grad(loss_fn, has_aux=True)(
                state.params
            )
            imb = imb_and_error[0]
            error = imb_and_error[1]

            # Update parameters
            state = state.apply_gradients(grads=grads)
            # L1 step (B. Carpenter et al, 2008)
            current_lr = self.lr_schedule(state.step)
            state_new = state.replace(
                params=jnp.where(state.params > 0, 1.0, 0.0)
                * jnp.maximum(0, state.params - current_lr * self.l1_strength)
                + jnp.where(state.params < 0, 1.0, 0.0)
                * jnp.minimum(0, state.params + current_lr * self.l1_strength)
            )
            return state_new, imb, error  # error always returned for compatibility

        # jit compilation of functions
        self._compute_rank_matrix_B = jax.jit(_compute_rank_matrix_B)
        self._cosine_decay_func = jax.jit(_cosine_decay_func)
        self._compute_point_adapt_lambdas = jax.jit(_compute_point_adapt_lambdas)
        self._compute_adapt_lambda = jax.jit(_compute_adapt_lambda)
        self._compute_lambda_decay = jax.jit(_compute_lambda_decay)
        self._compute_diff_imbalance = jax.jit(_compute_diff_imbalance)
        self._train_step = jax.jit(_train_step)

    def train(self):
        """Perform the full training of the DII, using the input attributes of the DiffImbalance object.

        Returns:
            params_output (jnp.array(float)): matrix of shape (num_epochs+1, n_features_A) containing the
                scaling weights during the whole training, starting from the initialization
            imbs_output (jnp.array(float)): array of shape (num_epochs+1,) containing the DII during the
                whole training
            errors_output (jnp.array(float)): array of shape (num_epochs+1,) containing the errors associated
                to the DII during the whole training. Only returned when return_error == True.
        """
        # Initialize optimizer
        self._init_optimizer()

        # Construct output arrays and initialize them using inital weights
        params_output = jnp.empty(shape=(self.num_epochs + 1, self.nfeatures_A))
        imbs_output = jnp.empty(shape=(self.num_epochs + 1,))
        errors_output = jnp.empty(shape=(self.num_epochs + 1,))
        batch_indices = jnp.arange(self.nrows // self.batches_per_epoch)
        imb_start, error_start = self._compute_diff_imbalance(
            self.init_params,
            self.data_A_rows[batch_indices],
            self.data_A_columns,
            self.ranks_B[batch_indices],
            0,
            batch_indices,
        )
        params_output = params_output.at[0].set(self.init_params)
        imbs_output = imbs_output.at[0].set(imb_start)
        errors_output = errors_output.at[0].set(error_start)

        # Train over different epochs
        for epoch_idx in tqdm(range(1, self.num_epochs + 1), desc="Training"):
            self.key, subkey = jax.random.split(self.key, num=2)
            params_now, imb_now, error_now = self._train_epoch(
                subkey
            )  # self.train_loader(subkey))
            params_output = params_output.at[epoch_idx].set(params_now)
            imbs_output = imbs_output.at[epoch_idx].set(imb_now)
            errors_output = imbs_output.at[epoch_idx].set(error_now)
        self.final_params = params_output[-1]
        if self.compute_error:
            return params_output, imbs_output, errors_output
        else:
            return params_output, imbs_output

    def _train_epoch(self, key):
        """Performs the training for a single epoch.

        Args:
            key (jax.random.PRNGKey): key for the Jax pseudo-random number generator (PRNG)

        Returns:
            params (jnp.array(float)): array of shape (n_features_A,) containing the
                weights at the last step of the current training epoch. The single mini-batch
                updates are not returned.
            imb (float): value of the DII at the last step of the current training epoch.
            error (float): error associated to the DII at the last step of the current training epoch.
                Only used when return_error == True, but always returned for compatibility.
        """
        all_batch_indices = jnp.split(
            jax.random.permutation(key, self.nrows), self.batches_per_epoch
        )

        # DON'T DELETE: ALTERNATIVE WAY, TO RESAMPLE POINTS IN TWO SUBSETS (ROWS AND COLUMNS)
        #if self.compute_error:
        #    self.key, subkey = jax.random.split(self.key, num=2)
        #    indices_rows = jax.random.choice(
        #        subkey, jnp.arange(self.data_A.shape[0]), shape=(self.nrows,), replace=False
        #    )
        #    indices_columns = jnp.delete(jnp.arange(self.data_A.shape[0]), indices_rows)
        #    self.data_A_rows = self.data_A[indices_rows]
        #    self.data_A_columns = self.data_A[indices_columns]
        #    self.data_B_rows = self.data_B[indices_rows]
        #    self.data_B_columns = self.data_B[indices_columns]
        #    self.ranks_B = self._compute_rank_matrix_B(
        #        batch_rows=self.data_B_rows,
        #        batch_columns=self.data_B_columns,
        #        periods=self.periods_B,
        #    )

        for batch_indices in all_batch_indices:
            self.state, imb, error = self._train_step(
                self.state,
                self.data_A_rows[batch_indices],
                self.data_A_columns,
                self.ranks_B[batch_indices],
                batch_indices,
            )

        # DON'T DELETE: ALTERNATIVE WAY FOR MINI-BATCH GD
        #for batch_indices in all_batch_indices:
        #    self.state, imb, error = self._train_step(
        #        self.state,
        #        self.data_A_rows[batch_indices],
        #        self.data_A_columns[batch_indices],
        #        self.ranks_B[batch_indices][:,batch_indices].argsort().argsort(),
        #        jnp.arange(batch_indices.shape[0]),
        #    )

        return self.state.params, imb, error

    def _init_optimizer(self):
        """Initialize the optimizer and the training state using the Optax library.

        The function uses the input attribute optimizer_name of the DiffImabalnce object,
        that can be set to one of the following options: "sgd", "adam", "adamw". For more
        information on these optimizers, see https://optax.readthedocs.io/en/latest/api/optimizers.html.

        """
        if self.optimizer_name.lower() == "adam":
            opt_class = optax.adam
        elif self.optimizer_name.lower() == "adamw":
            opt_class = optax.adamw
        elif self.optimizer_name.lower() == "sgd":
            opt_class = optax.sgd
        else:
            assert False, f'Unknown optimizer "{opt_class}"'
        # set the learning rate schedule (cosine decay or constant)
        if self.learning_rate_decay:
            self.lr_schedule = optax.cosine_decay_schedule(
                init_value=self.learning_rate,
                decay_steps=self.num_epochs * self.batches_per_epoch,
            )
        else:
            self.lr_schedule = optax.constant_schedule(
                value=self.learning_rate
            )
        optimizer = opt_class(self.lr_schedule)
        # Initialize training state
        self.state = train_state.TrainState.create(
            apply_fn=self._distance_A,
            params=self.init_params if self.state is None else self.state.params,
            tx=optimizer,
        )
