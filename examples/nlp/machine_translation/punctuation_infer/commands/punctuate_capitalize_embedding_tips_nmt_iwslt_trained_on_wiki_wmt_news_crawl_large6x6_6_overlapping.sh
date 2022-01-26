set -e
work_dir="~/data/iwslt/IWSLT-SLT/eval/en-de/IWSLT.tst2019/for_testing_punctuation_model2"
output_dir="${work_dir}/nmt_tips"
pred_labels="${output_dir}/embedding_tips_large6x6_6_overlapping_labels.txt"
python punctuate_capitalize_nmt.py \
    --input_text "${work_dir}/text_iwslt_en_text.txt" \
    --output_text "${output_dir}/embedding_tips_large6x6_6_overlapping_text.txt" \
    --output_labels "${pred_labels}" \
    --model_path ~/NWInf_results/autoregressive_punctuation_capitalization/tips_all_punc_no_u_nmt_wiki_wmt_news_crawl_large6x6_bs400000_steps400000_lr2e-4/checkpoints/AAYNLarge6x6.nemo \
    --max_seq_length 128 \
    --step 16 \
    --margin 16 \
    --batch_size 80 \
    --no_all_upper_label \
    --add_source_num_words_to_batch \
    --make_queries_contain_intact_sentences

python compute_metrics.py \
    --hyp ${pred_labels} \
    --ref "${work_dir}/labels_iwslt_en_text.txt" \
    --output "${output_dir}/embedding_tips_large6x6_6_overlapping_scores.json" \
    --normalize_punctuation_in_hyp \
    --reference_evelina_data_format
set +e