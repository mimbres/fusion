import yaml
import json
import pickle
import os
import random
import numpy as np
import sys
import argparse
from tqdm import tqdm
import pretty_midi
import torch
from torch.nn import functional as F
from transformers import EncoderDecoderModel
from torch.cuda import is_available as cuda_available
from aria.data.midi import MidiDict
from aria.tokenizer import AbsTokenizer

from corruptions import DataCorruption

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))
from utils.utils import flatten, unflatten, unflatten_corrupted, parse_generation, unflatten_for_aria, Segment_Novelty

# Parse command line arguments
parser = argparse.ArgumentParser()
parser.add_argument("--config", type=str, default=os.path.normpath("configs/configs_style_transfer.yaml"),
                    help="Path to the config file")
args = parser.parse_args()

# Load config file
with open(args.config, 'r') as f:
    configs = yaml.safe_load(f)
    
artifact_folder = configs["raw_data"]["artifact_folder"]
raw_data_folders = configs["raw_data"]["raw_data_folders"]
data_path = raw_data_folders['classical']['folder_path']
output_folder = os.path.join("/homes/kb658/fusion/output")
# Create output folder if it does not exist
if not os.path.exists(output_folder):
    os.makedirs(output_folder)

# Get tokenizer
tokenizer_filepath = os.path.join(artifact_folder, "fusion", "vocab_corrupted.pkl")
# Load the tokenizer dictionary
with open(tokenizer_filepath, "rb") as f:
    tokenizer = pickle.load(f)

# Reverse the tokenizer
decode_tokenizer = {v: k for k, v in tokenizer.items()}

# Load the fusion model
fusion_model = EncoderDecoderModel.from_pretrained(os.path.join(artifact_folder, "fusion", "style_transfer_700_epochs"))
fusion_model.eval()
fusion_model.to("cuda" if cuda_available() else "cpu")
print("Fusion model loaded")


def refine_sequence(corrupted_sequence, tokenizer, model, encoder_max_sequence_length, decoder_max_sequence_length):
    # Tokenize the sequence
    # input_tokens = [tokenizer[tuple(token)] if isinstance(token, list) else tokenizer[token] for token in corrupted_sequence]
    input_tokens = [tokenizer[token] for token in corrupted_sequence if token in tokenizer.keys()]
    # Pad the sequences
    if len(input_tokens) < encoder_max_sequence_length:
        input_tokens = F.pad(torch.tensor(input_tokens), (0, encoder_max_sequence_length - len(input_tokens))).to(torch.int64)
    else:
        input_tokens = torch.tensor(input_tokens[0:encoder_max_sequence_length]).to(torch.int64)

    # Attention mask based on non-padded tokens of the phrase
    attention_mask = torch.where(input_tokens != 0, 1, 0).type(torch.bool)

    # Generate the output sequence
    output_tokens = model.generate(input_ids=input_tokens.unsqueeze(0).to("cuda" if cuda_available() else "cpu"),
                                    attention_mask=attention_mask.unsqueeze(0).to("cuda" if cuda_available() else "cpu"),
                                    max_length=decoder_max_sequence_length,
                                    num_beams=1,
                                    early_stopping=False,
                                    do_sample=True,
                                    temperature=0.9,
                                    top_k=50,
                                    top_p=1.0,
                                    pad_token_id=0,
                                    eos_token_id=tokenizer["<E>"],
                                    bos_token_id=tokenizer["<S>"])

    # Decode the output tokens
    output_sequence = [decode_tokenizer[token.item()] for token in output_tokens[0]]

    generated_sequences = parse_generation(output_sequence, add_special_tokens = True)

    # Remove special tokens
    generated_sequences = [token for token in generated_sequences if token not in ["<S>", "<E>", "<SEP>"]]

    return generated_sequences


def generate_one_pass(tokenized_sequence, fusion_model, configs, corruption_type, corruption_rate):
    
    # Get the convert_to genre
    convert_to = configs['generation']['convert_to']

    # Get the encoder and decoder max sequence length
    encoder_max_sequence_length = configs['model']['fusion_model']['encoder_max_sequence_length']
    decoder_max_sequence_length = configs['model']['fusion_model']['decoder_max_sequence_length']
    
    
    corruption_obj = DataCorruption()
    separated_sequence = corruption_obj.seperateitems(tokenized_sequence)
    # Get the indices of the novelty tokens
    novelty_segments = [n for n, i in enumerate(separated_sequence) if '<N>' in i] + [n+1 for n, i in enumerate(separated_sequence) if '<n>' in i]
    all_segment_indices, _, _, _ = corruption_obj.get_segment_to_corrupt(separated_sequence, t_segment_ind=2, exclude_idx=novelty_segments)

    t_segment_ind = 2
    n_iterations = len(all_segment_indices) - 1
    # Initialize tqdm
    progress_bar = tqdm(total=n_iterations)
    jump_every = 1

    while t_segment_ind < n_iterations:

        if random.random() < corruption_rate:
            output_dict = corruption_obj.apply_random_corruption(tokenized_sequence, context_before=5, context_after=1, meta_data=[convert_to], t_segment_ind=t_segment_ind, inference=True, corruption_type=corruption_type)
            index = output_dict['index']
            corrupted_sequence = output_dict['corrupted_sequence']
            corrupted_sequence = unflatten_corrupted(corrupted_sequence)
            refined_segment = refine_sequence(corrupted_sequence, tokenizer, fusion_model, encoder_max_sequence_length, decoder_max_sequence_length)
            flattened_refined_segment = flatten(refined_segment, add_special_tokens=True)
            separated_sequence[index] = flattened_refined_segment
            tokenized_sequence = corruption_obj.concatenate_list(separated_sequence)
            print("Corrupted t_segment_ind:", t_segment_ind, "Corruption type:", output_dict['corruption_type'])
        
        t_segment_ind += jump_every
        # Update the progress bar
        progress_bar.update(jump_every)

    progress_bar.close()

    return tokenized_sequence

def get_midi_notes_from_tick(midi_dict, midi_data, peak_times):
    novel_note_numbers = []
    novel_notes = []
    for n, note in enumerate(midi_dict.note_msgs):
        if len(peak_times) > 0:
            tick = note['tick']
            # tick_to_time
            time_in_sec = pretty_midi.PrettyMIDI.tick_to_time(midi_data, tick)

            if time_in_sec > peak_times[0]:
                print(f"Note {n} is at {time_in_sec} seconds")
                novel_note_numbers.append(n)
                novel_notes.append(note)
                peak_times = np.delete(peak_times, 0)
        else:
            return novel_note_numbers, novel_notes

def add_novelty_segment_token(tokenized_sequence, novel_note_numbers):
    note_number = 0
    insertion_points = []
    for note_idx, note in enumerate(tokenized_sequence):
        if type(note) == list:
            if note_number in novel_note_numbers:
                insertion_points.append(note_idx)
            note_number += 1
    
    new_tokenized_sequence = []
    for idx, token in enumerate(tokenized_sequence):
        if idx in insertion_points:
            new_tokenized_sequence.append("<N>")
        new_tokenized_sequence.append(token)
    
    return new_tokenized_sequence

# Open JSON file
with open(os.path.join(artifact_folder, "fusion", "valid_file_list.json"), "r") as f:
    valid_sequences = json.load(f)

# Choose a random key whose value is classical from the valid sequences as file_path
valid_file_paths = [key for key, value in valid_sequences.items() if value == configs['generation']['convert_from']]
file_path = random.choice(valid_file_paths)
# file_path = os.path.join("/homes/kb658/fusion/input/pass_1_generated_Things Ain't What They Used To Be - Live At Maybeck Recital Hall, Berkeley, CA  January 20, 1991.midi")
file_path = "/homes/kb658/fusion/input/debussy-clair-de-lune.mid"
print("File path:", file_path)
mid = MidiDict.from_midi(file_path)
aria_tokenizer = AbsTokenizer()
tokenized_sequence = aria_tokenizer.tokenize(mid)

# # Save the original MIDI file
# filename = os.path.basename(file_path)
# mid_dict = aria_tokenizer.detokenize(tokenized_sequence)
# original_mid = mid_dict.to_midi()
# original_mid.save(os.path.join(output_folder, "original_" + filename))

# Get the instrument token
instrument_token = tokenized_sequence[0]
tokenized_sequence = tokenized_sequence[2:-1]

# Novelty based segmentation
audio_file = "/homes/kb658/fusion/input/debussy-clair-de-lune_original.wav"
config_file = "/homes/kb658/fusion/configs/config_ssm.yaml"
segment_novelty = Segment_Novelty(config_file, audio_file)
peak_times = segment_novelty.get_peak_timestamps(audio_file, 5)
pretty_midi_data = pretty_midi.PrettyMIDI(file_path)
novel_note_numbers, novel_notes = get_midi_notes_from_tick(mid, pretty_midi_data, peak_times)

# Flatten the tokenized sequence
tokenized_sequence = flatten(tokenized_sequence, add_special_tokens=True)

# Add novelty segment token to the tokenized sequence
tokenized_sequence = add_novelty_segment_token(tokenized_sequence, novel_note_numbers)

# Generate by iterating with multiple passes
passes = len(configs['generation']['passes'].keys())
for i in range(passes):
    print("Pass:", i + 1)
    corruption_type = configs['generation']['passes']['pass_' + str(i + 1)]['corruption_type']
    corruption_rate = configs['generation']['passes']['pass_' + str(i + 1)]['corruption_rate']
    # Generate the sequence
    tokenized_sequence = generate_one_pass(tokenized_sequence, fusion_model, configs, corruption_type, corruption_rate)

# Unflatten the tokenized sequence
tokenized_sequence = unflatten_for_aria(tokenized_sequence)

# Write the generated sequences to a MIDI file
generated_sequence = [('prefix', 'instrument', 'piano'), "<S>"] + tokenized_sequence + ["<E>"]
mid_dict = aria_tokenizer.detokenize(generated_sequence)
generated_mid = mid_dict.to_midi()
filename = os.path.basename(file_path)
generated_mid.save(os.path.join(output_folder, f"pass_{passes}_generated_" + filename))
