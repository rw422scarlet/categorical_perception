import time
import pprint
import numpy as np

class Logger():
    def __init__(self):
        self.epoch_dict = dict()
        self.history = []
        self.test_episodes = []
    
    def push(self, stats_dict):
        for key, val in stats_dict.items():
            if not (key in self.epoch_dict.keys()):
                self.epoch_dict[key] = []
            self.epoch_dict[key].append(val)

    def log(self):
        stats = dict()
        for key, val in self.epoch_dict.items():
            if isinstance(val[0], np.ndarray) or len(val) > 1:
                vals = np.stack(val)
                stats[key + "_avg"] = np.mean(vals)
                stats[key + "_std"] = np.std(vals)
                stats[key + "_min"] = np.min(vals)
                stats[key + "_max"] = np.max(vals)
            else:
                stats[key] = val[-1]

        pprint.pprint({k: np.round(v, 4) for k, v, in stats.items()})
        self.history.append(stats)

        # erase epoch stats
        self.epoch_dict = dict()
    
    def log_test_episode(self, sim_states, track_data):
        self.test_episodes.append({"sim_states": sim_states, "track_data": track_data})


def train(
    env, model, epochs, max_steps=500, steps_per_epoch=1000, 
    update_after=3000, update_every=50, custom_reward=None,
    verbose=False, callback=None
    ):
    """
    Args:
        env (Simulator): simulator environment
        model (Model): trainer model
        epochs (int): training epochs
        max_steps (int): 
        steps_per_epoch (int, optional): number of environment steps per epoch. Default=1000
        update_after (int, optional): initial burn-in steps before training. Default=3000
        update_every (int, optional): number of environment steps between training. Default=50
        custom_reward (class, optional): custom reward function
        verbose (bool, optional): whether to print instantaneous loss. Default=False
    """
    model.eval()
    logger = Logger()

    total_steps = epochs * steps_per_epoch
    start_time = time.time()
    
    model.reset()
    epoch = 0
    obs, eps_return, eps_len, done = env.reset(), 0, 0, False
    for t in range(total_steps):
        ctl = model.choose_action(obs)
        next_obs, reward, next_done, info = env.step(ctl)
        if custom_reward is not None:
            reward = custom_reward(next_obs)
        eps_return += reward
        eps_len += 1
        
        state = model.ref_agent._b.cpu().data.numpy()
        model.replay_buffer(obs, ctl, state, reward, done)
        obs = next_obs
        done = next_done
        
        # end of trajectory handeling
        if done or (eps_len + 1) >= max_steps:
            # collect terminal step
            ctl = model.choose_action(obs)
            state = model.ref_agent._b.cpu().data.numpy()
            model.replay_buffer(obs, ctl, state, reward, done)

            model.replay_buffer.push()
            logger.push({"eps_return": eps_return/eps_len})
            logger.push({"eps_len": eps_len})
            
            model.reset()
            obs, eps_return, eps_len, done = env.reset(), 0, 0, False

        # train model
        if t >= update_after and t % update_every == 0:
            train_stats = model.take_gradient_step(logger)

            if verbose:
                round_loss_dict = {k: round(v, 4) for k, v in train_stats.items()}
                print(f"e: {epoch}, t: {t}, {round_loss_dict}")

        # end of epoch handeling
        if (t + 1) % steps_per_epoch == 0:
            epoch = (t + 1) // steps_per_epoch

            logger.push({"epoch": epoch})
            logger.push({"time": time.time() - start_time})
            logger.log()
            print()
            
            model.on_epoch_end()

            if t > update_after and callback is not None:
                callback(model, logger)
    return model, logger
