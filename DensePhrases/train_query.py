from transformers import (
    AdamW,
    get_linear_schedule_with_warmup,
)
import json
import argparse
import torch
import os
import random
import numpy as np
import requests
import logging
import math
import copy
import string
import faiss

from time import time
from tqdm import tqdm
from densephrases.utils.squad_utils import get_question_dataloader, get_distill_dataloader
from densephrases.utils.single_utils import load_encoder
from densephrases.utils.open_utils import load_phrase_index, get_query2vec, load_qa_pairs
from densephrases.utils.eval_utils import drqa_exact_match_score, drqa_regex_match_score, \
    drqa_metric_max_over_ground_truths
from eval_phrase_retrieval import evaluate
from densephrases import Options

from spacy.lang.en import English
sentencizer = English()
sentencizer.add_pipe(sentencizer.create_pipe('sentencizer'))

from transformers import (
    AdamW,
    get_linear_schedule_with_warmup,
    AutoConfig,
    AutoModel,
)



logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s', datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler

def train_query_encoder(args, mips=None):
    # Freeze one for MIPS
    device = 'cuda' if args.cuda else 'cpu'
    logger.info("Loading pretrained encoder: this one is for MIPS (fixed)")
    pretrained_encoder, tokenizer, config = load_encoder(device, args)

    # Train a copy of it
    logger.info("Copying target encoder")
    target_encoder = copy.deepcopy(pretrained_encoder)

    # MIPS
    if mips is None:
        mips = load_phrase_index(args)

    # Optimizer setting
    def is_train_param(name):
        if name.endswith(".embeddings.word_embeddings.weight"):
            logger.info(f'freezing {name}')
            return False
        return True
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [{
        "params": [
            p for n, p in target_encoder.named_parameters()
            if not any(nd in n for nd in no_decay) and is_train_param(n)
        ],
        "weight_decay": 0.01,
    }, {
        "params": [
            p for n, p in target_encoder.named_parameters()
            if any(nd in n for nd in no_decay) and is_train_param(n)
        ],
        "weight_decay": 0.0
    },
    ]
    optimizer = AdamW(optimizer_grouped_parameters,
                      lr=args.learning_rate, eps=args.adam_epsilon)
    step_per_epoch = math.ceil(len(load_qa_pairs(args.train_path, args)[
                               1]) / args.per_gpu_train_batch_size)
    t_total = int(step_per_epoch //
                  args.gradient_accumulation_steps * args.num_train_epochs)
    logger.info(f"Train for {t_total} iterations")
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=t_total
    )
    eval_steps = math.ceil(
        len(load_qa_pairs(args.dev_path, args)[1]) / args.eval_batch_size)
    logger.info(f"Test takes {eval_steps} iterations")

    # Train arguments
    args.per_gpu_train_batch_size = int(
        args.per_gpu_train_batch_size / args.gradient_accumulation_steps)
    best_acc = -1000.0
        
    for ep_idx in range(int(args.num_train_epochs)):
        if args.distillation and args.distill_train:
            cross_encoder = torch.load(
                    os.path.join("/opt/ml/level3_nlp_finalproject-nlp-06/DensePhrases/outputs/spanbert-base-cased-nq", "pytorch_model.bin"), map_location=torch.device('cpu')
                    )
            new_qd = {n[len('bert')+1:]: p for n, p in cross_encoder.items() if 'bert' in n}
            new_linear = {n[len('qa_outputs')+1:]: p for n, p in cross_encoder.items() if 'qa_outputs' in n}
            qd_config, unused_kwargs = AutoConfig.from_pretrained(
                args.pretrained_name_or_path,
                cache_dir=args.cache_dir if args.cache_dir else None,
                return_unused_kwargs=True
            )
            qd_pretrained = AutoModel.from_pretrained(
                args.pretrained_name_or_path,
                config=qd_config,
                cache_dir=args.cache_dir if args.cache_dir else None,
            )
            target_encoder.cross_encoder = qd_pretrained
            target_encoder.cross_encoder.load_state_dict(new_qd)
            target_encoder.qa_outputs = torch.nn.Linear(config.hidden_size, 2)
            target_encoder.qa_outputs.load_state_dict(new_linear)
            
            target_encoder.cross_encoder.to(device)
            target_encoder.qa_outputs.to(device)

        # Training
        total_loss = 0.0
        total_accs = []
        total_accs_k = []

        # Load training dataset
        q_ids, questions, answers, titles, sentences, contexts = load_qa_pairs(
            args.train_path, args, shuffle=True)
        
        pbar = tqdm(get_top_phrases(
            mips, q_ids, questions, answers, titles, sentences, contexts, pretrained_encoder, tokenizer,
            args.per_gpu_train_batch_size, args)
        )

        for step_idx, (q_ids, questions, answers, titles, sentences, contexts, outs) in enumerate(pbar):
            svs, evs, tgts, p_tgts, c_tgts, s_tgts, start_positions, end_positions = annotate_phrase_vecs(mips, q_ids, questions, answers, titles, sentences, outs, args, contexts)
            if args.distillation and args.distill_train:
                examles_list = [{'id':q_id, 'question':question, 'context':context, 'titles':title, 'answers':answer, 'answer_start_idxs': start_pos, 'answer_end_idxs':end_pos}
                for q_id, question, context, title, answer, start_pos, end_pos in zip(q_ids, questions, contexts, titles, answers, start_positions, end_positions)]
                
                train_dataloader,all_stoken_index, all_etoken_index = get_distill_dataloader(examles_list, tokenizer, args)
                all_stoken_index_t = [torch.Tensor([sti_t if sti_t is not None else -1 for sti_t in stoken_index]).to(device) for stoken_index in all_stoken_index]
                all_etoken_index_t = [torch.Tensor([eti_t if eti_t is not None else -1 for eti_t in etoken_index]).to(device) for etoken_index in all_etoken_index]
                
            else:
                train_dataloader, _, _ = get_question_dataloader(
                    questions, tokenizer, args.max_query_length, batch_size=args.per_gpu_train_batch_size
                )
            
            target_encoder.train()
            svs_t = torch.Tensor(svs).to(device)
            evs_t = torch.Tensor(evs).to(device)
            tgts_t = [torch.Tensor([tgt_ for tgt_ in tgt if tgt_ is not None]).to(
                device) for tgt in tgts]
            p_tgts_t = [torch.Tensor([tgt_ for tgt_ in tgt if tgt_ is not None]).to(
                device) for tgt in p_tgts]
            c_tgts_t = [torch.Tensor([tgt_ for tgt_ in tgt if tgt_ is not None]).to(
                device) for tgt in p_tgts]
            s_tgts_t = [torch.Tensor([tgt_ for tgt_ in tgt if tgt_ is not None]).to(
                device) for tgt in s_tgts]

            assert len(train_dataloader) == 1

            for batch in train_dataloader:
                batch = tuple(t.to(device) for t in batch)
                if args.distillation and args.distill_train:
                    loss, accs = target_encoder.train_query(
                    input_ids_=batch[6], attention_mask_=batch[7], token_type_ids_=batch[8], # query
                    start_vecs=svs_t,
                    end_vecs=evs_t,
                    targets=tgts_t,
                    p_targets=p_tgts_t,
                    c_targets=c_tgts_t,
                    s_targets=s_tgts_t,
                    input_ids=batch[0], attention_mask=batch[1], token_type_ids=batch[2], # context
                    all_stoken_index=all_stoken_index_t, all_etoken_index=all_etoken_index_t
                    add_component=args.add_component,
                )
                else:
                    loss, accs = target_encoder.train_query(
                        input_ids_=batch[0], attention_mask_=batch[1], token_type_ids_=batch[2],
                        start_vecs=svs_t,
                        end_vecs=evs_t,
                        targets=tgts_t,
                        p_targets=p_tgts_t,
                    )

                # Optimize, get acc and report
                if loss is not None:
                    if args.gradient_accumulation_steps > 1:
                        loss = loss / args.gradient_accumulation_steps
                    if args.fp16:
                        with amp.scale_loss(loss, optimizer) as scaled_loss:
                            scaled_loss.backward()
                    else:
                        loss = torch.tensor([loss], requires_grad=True)
                        loss.backward()

                    total_loss += loss.mean().item()
                    if args.fp16:
                        torch.nn.utils.clip_grad_norm_(
                            amp.master_params(optimizer), args.max_grad_norm)
                    else:
                        torch.nn.utils.clip_grad_norm_(
                            target_encoder.parameters(), args.max_grad_norm)

                    optimizer.step()
                    scheduler.step()  # Update learning rate schedule
                    target_encoder.zero_grad()

                    pbar.set_description(
                        f"Ep {ep_idx+1} Tr loss: {loss.mean().item():.2f}, acc: {sum(accs)/len(accs):.3f}"
                    )

                if accs is not None:
                    total_accs += accs
                    total_accs_k += [len(tgt) > 0 for tgt in tgts_t]
                else:
                    total_accs += [0.0]*len(tgts_t)
                    total_accs_k += [0.0]*len(tgts_t)

        step_idx += 1
        logger.info(
            f"Avg train loss ({step_idx} iterations): {total_loss/step_idx:.2f} | train " +
            f"acc@1: {sum(total_accs)/len(total_accs):.3f} | acc@{args.top_k}: {sum(total_accs_k)/len(total_accs_k):.3f}"
        )

        args.distill_train = False
        # Evaluation
        new_args = copy.deepcopy(args)
        new_args.top_k = 10
        new_args.save_pred = False
        new_args.test_path = args.dev_path
        dev_em, dev_f1, dev_emk, dev_f1k = evaluate(
            new_args, mips, target_encoder, tokenizer)
        logger.info(f"Develoment set acc@1: {dev_em:.3f}, f1@1: {dev_f1:.3f}")

        args.distill_train = True
        
        # Save best model
        if dev_em > best_acc:
            best_acc = dev_em
            save_path = args.output_dir
            if not os.path.exists(save_path):
                os.makedirs(save_path)
            # Remove teacher before saving
            if args.distillation:
                del target_encoder.cross_encoder
                del target_encoder.qa_outputs
            target_encoder.save_pretrained(save_path)
            logger.info(
                f"Saved best model with acc {best_acc:.3f} into {save_path}")

        if (ep_idx + 1) % 1 == 0:
            logger.info('Updating pretrained encoder')
            pretrained_encoder = copy.deepcopy(target_encoder)

    print()
    logger.info(f"Best model has acc {best_acc:.3f} saved as {save_path}")


def get_top_phrases(mips, q_ids, questions, answers, titles, sentences, contexts, query_encoder, tokenizer, batch_size, args):
    # Search
    step = batch_size
    phrase_idxs = []
    search_fn = mips.search

    query2vec = get_query2vec(
        query_encoder=query_encoder, tokenizer=tokenizer, args=args, batch_size=batch_size
    )
    for q_idx in tqdm(range(0, len(questions), step)):
        outs = query2vec(questions[q_idx:q_idx+step]) # batch size 만큼 question 가지고 와서 vector 화
        start = np.concatenate([out[0] for out in outs], 0) # start position 찾을 vector
        end = np.concatenate([out[1] for out in outs], 0) # end position 찾을 vector
        query_vec = np.concatenate([start, end], 1)

        outs = search_fn(
            query_vec,
            q_texts=questions[q_idx:q_idx+step], nprobe=args.nprobe,
            top_k=args.top_k, return_idxs=True,
            max_answer_length=args.max_answer_length, aggregate=args.aggregate, agg_strat=args.agg_strat,
        )
        yield (
            q_ids[q_idx:q_idx+step], questions[q_idx:q_idx +
                                               step], answers[q_idx:q_idx+step],
            titles[q_idx:q_idx+step],
            sentences[q_idx:q_idx+step], contexts[q_idx:q_idx+step],
            outs
        )


def annotate_phrase_vecs(mips, q_ids, questions, answers, titles, sentences, contexts, phrase_groups, args):
    assert mips is not None
    batch_size = len(answers)
    # Phrase groups are in size of [batch, top_k, values]
    # phrase_groups = [[(
    #     out_['doc_idx'], out_['start_idx'], out_['end_idx'], out_['answer'],
    #     out_['start_vec'], out_['end_vec'], out_['context'], out_['title'])
    #     for out_ in out] for out in outs
    # ]

    dummy_group = {
        'doc_idx': -1,
        'start_idx': 0, 'end_idx': 0,
        'answer': '',
        'start_vec': np.zeros(768),
        'end_vec': np.zeros(768),
        'context': '', 'title': [''],
        'sentence': ['']
    }

    # get sentence information
    for group_idx, phrase_group in enumerate(phrase_groups):
        for sample_idx, sample in enumerate(phrase_group):
            sents = [(X.text, X[0].idx)
                     for X in sentencizer(sample['context']).sents]
            get_sent_range = [i[1] for i in sents]
            sent_pos = 0
            for i in range(len(get_sent_range)):
                if i != (len(get_sent_range) - 1):
                    if get_sent_range[i] <= sample['start_pos'] and get_sent_range[i+1] > sample['end_pos']:
                        sent_pos = i
                elif i == (len(get_sent_range) - 1):
                    if get_sent_range[i] <= sample['start_pos'] and (len(get_sent_range)-1) > sample['end_pos']:
                        sent_pos = i
            phrase_groups[group_idx][sample_idx]['sentence'] = sents[sent_pos][0]

    # Pad phrase groups (two separate top-k coming from start/end, so pad with top_k*2)

    for b_idx, phrase_idx in enumerate(phrase_groups):
        while len(phrase_groups[b_idx]) < args.top_k*2:
            phrase_groups[b_idx].append(dummy_group)
        assert len(phrase_groups[b_idx]) == args.top_k*2

    # Flatten phrase groups
    flat_phrase_groups = [
        phrase for phrase_group in phrase_groups for phrase in phrase_group]
    doc_idxs = [int(phrase_group['doc_idx'])
                for phrase_group in flat_phrase_groups]
    start_vecs = [phrase_group['start_vec']
                  for phrase_group in flat_phrase_groups]
    end_vecs = [phrase_group['end_vec'] for phrase_group in flat_phrase_groups]

    # stack vectors
    start_vecs = np.stack(start_vecs)
    end_vecs = np.stack(end_vecs)
    zero_mask = np.array([[1] if doc_idx >= 0 else [0]
                         for doc_idx in doc_idxs])
    start_vecs = start_vecs * zero_mask
    end_vecs = end_vecs * zero_mask

    # Reshape
    start_vecs = np.reshape(start_vecs, (batch_size, args.top_k*2, -1))
    end_vecs = np.reshape(end_vecs, (batch_size, args.top_k*2, -1))

    # Dummy targets
    targets = [[None for phrase in phrase_group]
               for phrase_group in phrase_groups]
    p_targets = [[None for phrase in phrase_group]
                 for phrase_group in phrase_groups]
    c_targets = [[None for phrase in phrase_group]
                 for phrase_group in phrase_groups]
    s_targets = [[None for phrase in phrase_group]
                 for phrase_group in phrase_groups]

    # TODO: implement dynamic label_strategy based on the task name (label_strat = dynamic)

    # Annotate for L_phrase
    if 'phrase' in args.label_strat.split(','): #(12, 200 11) -> 11개가 각각의 phrase group 이네
        match_fns = [
            drqa_regex_match_score if args.regex or ('trec' in q_id.lower()) else drqa_exact_match_score for q_id in q_ids
        ]
        targets = [
            [drqa_metric_max_over_ground_truths(
                match_fn, phrase['answer'], answer_set) for phrase in phrase_group]
            for phrase_group, answer_set, match_fn in zip(phrase_groups, answers, match_fns)
        ]

        targets = [[ii if val else None for ii,
                    val in enumerate(target)] for target in targets]

    # Annotate for L_doc
    if 'doc' in args.label_strat.split(','):
        p_targets = [
            [any(phrase['title'][0].lower() == tit.lower()
                 for tit in title) for phrase in phrase_group]
            for phrase_group, title in zip(phrase_groups, titles)
        ]
        p_targets = [[ii if val else None for ii,
                      val in enumerate(target)] for target in p_targets]

    # Annotate for L_context
    if 'context' in args.label_strat.split(','):
        c_targets = [
            [context[1:] in phrase['context'] for phrase in phrase_group]
            for phrase_group, context in zip(phrase_groups, contexts)
        ]

        c_targets = [[ii if val else None for ii,
                      val in enumerate(target)] for target in c_targets]

    # Annotate for L_sentence
    if 'sentence' in args.label_strat.split(','):
        s_targets = [
            [any(phrase['sentence'].lower() in sent.lower()
                 for sent in sentence) for phrase in phrase_group]
            for phrase_group, sentence in zip(phrase_groups, sentences)
        ]
        s_targets = [[ii if val else None for ii,
                      val in enumerate(target)] for target in s_targets]

    return start_vecs, end_vecs, targets, p_targets, c_targets, s_targets

    start_positions = None
    end_positions = None
    
    # Annotate for distillation
    if args.distillation:
        start_positions = []
        end_positions = []
        for phrase_group, context in zip(phrase_groups, contexts):
            start_position = []
            end_position = []
            for phrase in phrase_group:
                if context.strip().lower() in phrase['context'].strip().lower(): # retrieved phrase is in the gold context
                    start_position.append(phrase['start_pos'])
                    end_position.append(phrase['end_pos']-1)
                else:
                    start_position.append(None)
                    end_position.append(None)
            start_positions.append(start_position)
            end_positions.append(end_position)
                    

    return start_vecs, end_vecs, targets, p_targets, start_positions, end_positions


if __name__ == '__main__':
    # See options in densephrases.options
    options = Options()
    options.add_model_options()
    options.add_index_options()
    options.add_retrieval_options()
    options.add_data_options()
    options.add_qsft_options()
    args = options.parse()

    # Seed for reproducibility
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    if args.run_mode == 'train_query':
        # Train
        mips = load_phrase_index(args)
        train_query_encoder(args, mips)
       
        
        # Eval
        args.load_dir = args.output_dir
        logger.info(f"Evaluating {args.load_dir}")
        args.top_k = 10
        evaluate(args, mips)

    else:
        raise NotImplementedError
