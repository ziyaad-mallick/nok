import numpy as np
import scipy.signal as signal
import math

def generate_synthetic_audio(sample_rate=16000):
    """
    Generates synthetic PCM audio containing:
    1. Isolated Tongue Pop (vacuum release impulse of damped resonator in silence)
    2. Speech Plosive 'P' in 'PA' (unvoiced burst followed immediately by voiced vowel formants)
    3. Speech Plosive 'T' in 'TOP' (high freq burst followed by vowel formants)
    4. Speech Fricative 'S' in 'STOP' (sustained high freq noise + burst + vowel)
    5. Pure Silence / Ambient Noise
    """
    duration = 5.0 # 5 seconds
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    audio = np.random.normal(0, 50, len(t)) # Low background noise

    # Event 1: Isolated Tongue Pop at t = 1.0s
    # Model: Damped sinusoid (impulse response of closed cavity, f_res = 1800 Hz, decay = 8ms)
    pop_t = t - 1.0
    pop_mask = (pop_t >= 0) & (pop_t < 0.015) # 15ms duration
    pop_wave = np.sin(2 * np.pi * 1800 * pop_t[pop_mask]) * np.exp(-pop_t[pop_mask] / 0.003) * 15000.0
    audio[pop_mask] += pop_wave

    # Event 2: Speech Word 'PA' at t = 2.0s
    # Plosive burst 'P': 20ms broadband noise
    p_burst_t = t - 2.0
    p_mask = (p_burst_t >= 0) & (p_burst_t < 0.020)
    p_burst = np.random.normal(0, 8000, np.sum(p_mask)) * np.exp(-p_burst_t[p_mask] / 0.01)
    audio[p_mask] += p_burst
    
    # Voiced Vowel 'AH' (Formants at 700Hz, 1200Hz, Fundamental f0 = 130Hz) starting at t = 2.02s (20ms after burst)
    vowel_t = t - 2.02
    vowel_mask = (vowel_t >= 0) & (vowel_t < 0.250) # 250ms vowel
    vowel_wave = (np.sin(2 * np.pi * 130 * vowel_t[vowel_mask]) * 0.5 +
                  np.sin(2 * np.pi * 700 * vowel_t[vowel_mask]) * 0.3 +
                  np.sin(2 * np.pi * 1200 * vowel_t[vowel_mask]) * 0.2) * 12000.0
    # Envelope
    vowel_env = np.hanning(len(vowel_wave))
    audio[vowel_mask] += vowel_wave * vowel_env

    # Event 3: Speech Word 'STOP' at t = 3.5s
    # 'S': Fricative 80ms high-freq noise (4kHz - 7kHz)
    s_t = t - 3.5
    s_mask = (s_t >= 0) & (s_t < 0.080)
    s_wave = np.random.normal(0, 6000, np.sum(s_mask))
    audio[s_mask] += s_wave

    # 'T' burst at t = 3.59s
    t_burst_t = t - 3.59
    t_mask = (t_burst_t >= 0) & (t_burst_t < 0.025)
    t_burst = np.random.normal(0, 9000, np.sum(t_mask))
    audio[t_mask] += t_burst

    # Vowel 'AH' at t = 3.615s
    v2_t = t - 3.615
    v2_mask = (v2_t >= 0) & (v2_t < 0.200)
    v2_wave = (np.sin(2 * np.pi * 140 * v2_t[v2_mask]) * 0.5 +
               np.sin(2 * np.pi * 800 * v2_t[v2_mask]) * 0.4) * 10000.0
    audio[v2_mask] += v2_wave * np.hanning(len(v2_wave))

    return audio, sample_rate

def test_original_naive_detector(audio, sample_rate, chunk_size=512):
    """
    Original algorithm from Section 5:
    Energy threshold + High frequency energy sum > 100,000
    """
    triggers = []
    energy_threshold = 4000.0
    
    num_chunks = len(audio) // chunk_size
    for i in range(num_chunks):
        chunk = audio[i*chunk_size : (i+1)*chunk_size]
        time_sec = (i * chunk_size) / sample_rate
        
        energy = np.sqrt(np.mean(chunk**2))
        fft_data = np.abs(np.fft.rfft(chunk))
        high_freq_energy = np.sum(fft_data[int(len(fft_data)*0.3):])
        
        if energy > energy_threshold and high_freq_energy > 100000:
            triggers.append((time_sec, energy, high_freq_energy))
            
    return triggers

def test_gated_dsp_detector(audio, sample_rate, chunk_size=512):
    """
    Advanced VAD-Gated & Isolated Transient Detector:
    1. Check if high-freq transient occurs.
    2. Check for Voiced Speech Energy (f0 fundamental frequency harmonics between 80Hz-300Hz) in surrounding [-150ms, +150ms] window.
    3. If voiced speech or pre-burst/post-burst energy is present, REJECT (it's speech!).
    4. Only trigger if isolated in silence.
    """
    triggers = []
    num_chunks = len(audio) // chunk_size
    
    for i in range(2, num_chunks - 2):
        chunk = audio[i*chunk_size : (i+1)*chunk_size]
        time_sec = (i * chunk_size) / sample_rate
        
        # Current chunk metrics
        energy = np.sqrt(np.mean(chunk**2))
        fft_data = np.abs(np.fft.rfft(chunk))
        freqs = np.fft.rfftfreq(chunk_size, 1/sample_rate)
        
        high_freq_energy = np.sum(fft_data[freqs > 1500])
        
        # Check candidate transient
        if energy > 2000.0 and high_freq_energy > 80000:
            # Inspection window: 150ms before and 150ms after (approx 5 chunks before/after)
            surrounding_chunks = audio[max(0, (i-4)*chunk_size) : min(len(audio), (i+5)*chunk_size)]
            
            # Check for voiced energy (autocorrelation peak for pitch in 80Hz-300Hz)
            # Autocorrelation
            corr = np.correlate(surrounding_chunks, surrounding_chunks, mode='full')
            corr = corr[len(corr)//2:]
            
            min_lag = int(sample_rate / 300) # ~53 samples
            max_lag = int(sample_rate / 80)  # 200 samples
            
            autocorr_max = np.max(corr[min_lag:max_lag]) if len(corr) > max_lag else 0
            autocorr_zero = corr[0] if corr[0] > 0 else 1.0
            pitch_strength = autocorr_max / autocorr_zero
            
            # Check pre-energy and post-energy (speech plosives are followed by vowels)
            post_chunk = audio[(i+2)*chunk_size : (i+4)*chunk_size]
            post_energy = np.sqrt(np.mean(post_chunk**2)) if len(post_chunk) > 0 else 0
            
            # If pitch strength is low AND post-burst energy is low (isolated in silence), accept as POP!
            if pitch_strength < 0.45 and post_energy < 1500.0:
                triggers.append((time_sec, pitch_strength, post_energy))
                
    return triggers

if __name__ == "__main__":
    audio, sr = generate_synthetic_audio()
    
    print("=== TESTING NAIVE DETECTOR (FROM SECTION 5 CODE) ===")
    naive_triggers = test_original_naive_detector(audio, sr)
    for t in naive_triggers:
        print(f"Trigger at t={t[0]:.3f}s (Energy={t[1]:.1f}, HighFreq={t[2]:.1f})")
        
    print("\n=== TESTING ADVANCED VAD-GATED DETECTOR ===")
    gated_triggers = test_gated_dsp_detector(audio, sr)
    for t in gated_triggers:
        print(f"Trigger at t={t[0]:.3f}s (PitchStrength={t[1]:.3f}, PostEnergy={t[2]:.1f})")
