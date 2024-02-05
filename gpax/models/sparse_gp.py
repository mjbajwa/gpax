"""
sparse_gp.py
=======

Variational inference implementation of sparse Gaussian process regression

Created by Maxim Ziatdinov (email: maxim.ziatdinov@gmail.com)
"""

from typing import Callable, Dict, Optional, Tuple, Type

import jax
import jaxlib
import jax.numpy as jnp
from jax.scipy.linalg import cholesky, solve_triangular

import numpyro
import numpyro.distributions as dist
from numpyro.infer import SVI, Trace_ELBO
from numpyro.infer.autoguide import AutoDelta, AutoNormal

from .gp import ExactGP


class viSparseGP(ExactGP):
    """
    Variational inference based sparse Gaussian process

    Args:
        input_dim:
            Number of input dimensions
        kernel:
            Kernel function ('RBF', 'Matern', 'Periodic', or custom function)
        mean_fn:
            Optional deterministic mean function (use 'mean_fn_priors' to make it probabilistic)
        kernel_prior:
            Optional custom priors over kernel hyperparameters; uses LogNormal(0,1) by default
        mean_fn_prior:
            Optional priors over mean function parameters
        noise_prior:
            Optional custom prior for the observation noise variance; uses LogNormal(0,1) by default.
        guide:
            Auto-guide option, use 'delta' (default) or 'normal'
    """
    def __init__(self, input_dim: int, kernel: str,
                 mean_fn: Optional[Callable[[jnp.ndarray, Dict[str, jnp.ndarray]], jnp.ndarray]] = None,
                 kernel_prior: Optional[Callable[[], Dict[str, jnp.ndarray]]] = None,
                 mean_fn_prior: Optional[Callable[[], Dict[str, jnp.ndarray]]] = None,
                 noise_prior: Optional[Callable[[], Dict[str, jnp.ndarray]]] = None,
                 noise_prior_dist: Optional[dist.Distribution] = None,
                 lengthscale_prior_dist: Optional[dist.Distribution] = None,
                 guide: str = 'delta') -> None:
        args = (input_dim, kernel, mean_fn, kernel_prior, mean_fn_prior, noise_prior,
                noise_prior_dist, lengthscale_prior_dist)
        super(viSparseGP, self).__init__(*args)
        self.X_train = None
        self.y_train = None
        self.Xu = None
        self.guide_type = AutoNormal if guide == 'normal' else AutoDelta
        self.svi = None

    def model(self, X: jnp.ndarray, y: jnp.ndarray = None, **kwargs: float) -> None:
        
        # Initialize mean function at zeros
        f_loc = jnp.zeros(X.shape[0])
        # Sample kernel parameters
        if self.kernel_prior:
            kernel_params = self.kernel_prior()
        else:
            kernel_params = self._sample_kernel_params()
        # Sample noise
        if self.noise_prior:  # this will be removed in the future releases
            noise = self.noise_prior()
        else:
            noise = self._sample_noise()
        D = jnp.broadcast_to(noise, (X.shape[0],) )
        # Add mean function (if any)
        if self.mean_fn is not None:
            args = [X]
            if self.mean_fn_prior is not None:
                args += [self.mean_fn_prior()]
            f_loc += self.mean_fn(*args).squeeze()
        # compute kernel between inducing points
        Kuu = self.kernel(self.Xu, self.Xu, kernel_params) 
        # Cholesky decomposition
        Luu = cholesky(Kuu).T
        # Kernel computation
        Kuf = self.kernel(self.Xu, X, kernel_params)
        # Solve triangular system
        W = solve_triangular(Luu, Kuf, lower=True).T
        # Diagonal of the kernel matrix
        Kffdiag = jnp.diag(self.kernel(X, X, kernel_params, jitter=0))
        # Sum of squares computation
        Qffdiag = jnp.square(W).sum(axis=-1)
        # Trace term computation
        trace_term = (Kffdiag - Qffdiag).sum() / noise
        # Clamping the trace term
        trace_term = jnp.clip(trace_term, a_min=0)

        # VFE
        numpyro.factor("trace_term", -trace_term / 2.0)

        numpyro.sample(
            "y",
            dist.LowRankMultivariateNormal(loc=f_loc, cov_factor=W, cov_diag=D),
            obs=y)

    def fit(self,
            rng_key: jnp.array, X: jnp.ndarray, y: jnp.ndarray, Xu: jnp.ndarray,
            num_steps: int = 1000, step_size: float = 5e-3,
            progress_bar: bool = True, print_summary: bool = True,
            device: Type[jaxlib.xla_extension.Device] = None,
            **kwargs: float
            ) -> None:
        """
        Run variational inference to learn GP (hyper)parameters

        Args:
            rng_key: random number generator key
            X: 2D feature vector with *(number of points, number of features)* dimensions
            y: 1D target vector with *(n,)* dimensions
            num_steps: number of SVI steps
            step_size: step size schedule for Adam optimizer
            progress_bar: show progress bar
            print_summary: print summary at the end of training
            device:
                optionally specify a cpu or gpu device on which to run the inference;
                e.g., ``device=jax.devices("cpu")[0]``
            **jitter:
                Small positive term added to the diagonal part of a covariance
                matrix for numerical stability (Default: 1e-6)
        """
        X, y = self._set_data(X, y)
        if device:
            X = jax.device_put(X, device)
            y = jax.device_put(y, device)
        self.X_train = X
        self.y_train = y

        self.Xu = numpyro.param("Xu", Xu)

        optim = numpyro.optim.Adam(step_size=step_size, b1=0.5)
        self.svi = SVI(
            self.model,
            guide=self.guide_type(self.model),
            optim=optim,
            loss=Trace_ELBO(),
            X=X,
            y=y,
            **kwargs
        )

        self.kernel_params = self.svi.run(
            rng_key, num_steps, progress_bar=progress_bar)[0]

        if print_summary:
            self._print_summary()

    def get_samples(self) -> Dict[str, jnp.ndarray]:
        """Get posterior samples"""
        return self.svi.guide.median(self.kernel_params)

    def predict_in_batches(self, rng_key: jnp.ndarray,
                           X_new: jnp.ndarray,  batch_size: int = 100,
                           samples: Optional[Dict[str, jnp.ndarray]] = None,
                           predict_fn: Callable[[jnp.ndarray, int], Tuple[jnp.ndarray]] = None,
                           noiseless: bool = False,
                           device: Type[jaxlib.xla_extension.Device] = None,
                           **kwargs: float
                           ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        """
        Make prediction at X_new with sampled GP parameters
        by spitting the input array into chunks ("batches") and running
        predict_fn (defaults to self.predict) on each of them one-by-one
        to avoid a memory overflow
        """
        predict_fn = lambda xi:  self.predict(
                rng_key, xi, samples, noiseless, **kwargs)
        y_pred, y_var = self._predict_in_batches(
            rng_key, X_new, batch_size, 0, samples,
            predict_fn=predict_fn, noiseless=noiseless,
            device=device, **kwargs)
        y_pred = jnp.concatenate(y_pred, 0)
        y_var = jnp.concatenate(y_var, 0)
        return y_pred, y_var

    def predict(self, rng_key: jnp.ndarray, X_new: jnp.ndarray,
                samples: Optional[Dict[str, jnp.ndarray]] = None,
                noiseless: bool = False,
                device: Type[jaxlib.xla_extension.Device] = None, **kwargs: float
                ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        """
        Make prediction at X_new points using posterior samples for GP parameters

        Args:
            rng_key: random number generator key
            X_new: new inputs with *(number of points, number of features)* dimensions
            noiseless:
                Noise-free prediction. It is set to False by default as new/unseen data is assumed
                to follow the same distribution as the training data. Hence, since we introduce a model noise
                by default for the training data, we also want to include that noise in our prediction.
            device:
                optionally specify a cpu or gpu device on which to make a prediction;
                e.g., ```device=jax.devices("gpu")[0]```
            **jitter:
                Small positive term added to the diagonal part of a covariance
                matrix for numerical stability (Default: 1e-6)

        Returns
            Center of the mass of sampled means and all the sampled predictions
        """
        X_new = self._set_data(X_new)
        if device:
            self._set_training_data(device=device)
            X_new = jax.device_put(X_new, device)
        if samples is None:
            samples = self.get_samples()
        mean, cov = self.get_mvn_posterior(X_new, samples, noiseless, **kwargs)
        return mean, cov.diagonal()

    def _print_summary(self) -> None:
        params_map = self.get_samples()
        print('\nInferred GP parameters')
        for (k, vals) in params_map.items():
            spaces = " " * (15 - len(k))
            print(k, spaces, jnp.around(vals, 4))
