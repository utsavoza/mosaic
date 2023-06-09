import copy

import tensorflow as tf
import tensorflow.experimental.numpy as tnp
from keras_cv.models.stable_diffusion.noise_scheduler import NoiseScheduler


class Trainer(tf.keras.Model):
    # Adapted from https://github.com/huggingface/diffusers/blob/main/examples/text_to_image/train_text_to_image.py

    def __init__(
        self,
        diffusion_model: tf.keras.Model,
        vae: tf.keras.Model,
        noise_scheduler: NoiseScheduler,
        pretrained_ckpt: str,
        mp: bool,
        ema=0.9999,
        max_grad_norm=1.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.diffusion_model = diffusion_model
        if pretrained_ckpt is not None:
            self.diffusion_model.load_weights(pretrained_ckpt)
            print(
                f"Loading the provided checkpoint to initialize the diffusion model: {pretrained_ckpt}..."
            )

        self.vae = vae
        self.noise_scheduler = noise_scheduler

        if ema > 0.0:
            self.ema = tf.Variable(ema, dtype="float32")
            self.optimization_step = tf.Variable(0, dtype="int32")
            self.ema_diffusion_model = copy.deepcopy(self.diffusion_model)
            self.do_ema = True
        else:
            self.do_ema = False

        self.vae.trainable = False
        self.mp = mp
        self.max_grad_norm = max_grad_norm

    def train_step(self, inputs):
        images = inputs["images"]
        encoded_text = inputs["encoded_text"]
        bsz = tf.shape(images)[0]

        with tf.GradientTape() as tape:
            # Project image into the latent space.
            latents = self.sample_from_encoder_outputs(
                self.vae(images, training=False))
            latents = latents * 0.18215

            # Sample noise that we'll add to the latents
            noise = tf.random.normal(tf.shape(latents))

            # Sample a random timestep for each image
            timesteps = tnp.random.randint(
                0, self.noise_scheduler.train_timesteps, (bsz,)
            )

            # Add noise to the latents according to the noise magnitude at each timestep
            # (this is the forward diffusion process)
            noisy_latents = self.noise_scheduler.add_noise(
                tf.cast(latents, noise.dtype), noise, timesteps
            )

            # Get the target for loss depending on the prediction type
            # just the sampled noise for now.
            target = noise  # noise_schedule.predict_epsilon == True

            # Can be implemented:
            # https://github.com/huggingface/diffusers/blob/9be94d9c6659f7a0a804874f445291e3a84d61d4/src/diffusers/schedulers/scheduling_ddpm.py#L352

            # Predict the noise residual and compute loss
            timestep_embeddings = tf.map_fn(
                lambda t: self.get_timestep_embedding(t), timesteps, dtype=tf.float32
            )
            timestep_embeddings = tf.squeeze(timestep_embeddings, 1)
            model_pred = self.diffusion_model(
                [noisy_latents, timestep_embeddings, encoded_text], training=True
            )
            loss = self.compiled_loss(target, model_pred)
            if self.mp:
                loss = self.optimizer.get_scaled_loss(loss)

        # Update parameters of the diffusion model.
        trainable_vars = self.diffusion_model.trainable_variables
        gradients = tape.gradient(loss, trainable_vars)
        if self.mp:
            gradients = self.optimizer.get_unscaled_gradients(gradients)
        if self.max_grad_norm > 0.0:
            gradients = [tf.clip_by_norm(g, self.max_grad_norm)
                         for g in gradients]
        self.optimizer.apply_gradients(zip(gradients, trainable_vars))

        # EMA.
        if self.do_ema:
            self.ema_step()

        return {m.name: m.result() for m in self.metrics}

    def get_timestep_embedding(self, timestep, dim=320, max_period=10000):
        # Taken from
        # # https://github.com/keras-team/keras-cv/blob/ecfafd9ea7fe9771465903f5c1a03ceb17e333f1/keras_cv/models/stable_diffusion/stable_diffusion.py#L481
        half = dim // 2
        log_max_period = tf.math.log(tf.cast(max_period, tf.float32))
        freqs = tf.math.exp(-log_max_period * tf.range(0,
                            half, dtype=tf.float32) / half)
        args = tf.convert_to_tensor([timestep], dtype=tf.float32) * freqs
        embedding = tf.concat([tf.math.cos(args), tf.math.sin(args)], 0)
        embedding = tf.reshape(embedding, [1, -1])
        return embedding  # Excluding the repeat.

    def get_decay(self, optimization_step):
        value = (1 + optimization_step) / (10 + optimization_step)
        value = tf.cast(value, dtype=self.ema.dtype)
        return 1 - tf.math.minimum(self.ema, value)

    def ema_step(self):
        self.optimization_step.assign_add(1)
        self.ema.assign(self.get_decay(self.optimization_step))

        for weight, ema_weight in zip(
            self.diffusion_model.trainable_variables,
            self.ema_diffusion_model.trainable_variables,
        ):
            tmp = self.ema * (ema_weight - weight)
            ema_weight.assign_sub(tmp)

    def sample_from_encoder_outputs(self, outputs):
        mean, logvar = tf.split(outputs, 2, axis=-1)
        logvar = tf.clip_by_value(logvar, -30.0, 20.0)
        std = tf.exp(0.5 * logvar)
        sample = tf.random.normal(tf.shape(mean), dtype=mean.dtype)
        return mean + std * sample

    def save_weights(self, filepath, overwrite=True, save_format=None, options=None):
        # Overriding to help with the `ModelCheckpoint` callback.
        if self.do_ema:
            self.ema_diffusion_model.save_weights(
                filepath=filepath,
                overwrite=overwrite,
                save_format=save_format,
                options=options,
            )
        else:
            self.diffusion_model.save_weights(
                filepath=filepath,
                overwrite=overwrite,
                save_format=save_format,
                options=options,
            )
