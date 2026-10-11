import unittest
from app.extractor import boxes, gender_candidates, looks_like_card, parse_target_text, serial_candidates, has_voter_card_evidence
from app.ocr import epic_candidates, epic_matches, parse_fields, select_voter_id, serial_header_candidate
from app.reconcile import reconcile
from PIL import Image

class PipelineTests(unittest.TestCase):
    def test_voter_id_pattern_is_candidate_only(self):
        self.assertEqual(epic_matches('ABC1234567'), ['ABC1234567'])
        self.assertEqual(epic_matches('ABC123'), [])

    def test_voter_id_confusions_are_candidates_not_silent_repairs(self):
        self.assertEqual(epic_candidates('YWBOS50665'), ['YWB0550665'])

    def test_select_id_from_raw_text_when_target_crop_has_multiple_candidates(self):
        selected, reason=select_voter_id('ABC1234567','ABC1234567 ABD1234567',['ABC1234567','ABD1234567'])
        self.assertEqual((selected,reason),('ABC1234567','EXACT_CARD_HEADER'))

    def test_card_header_id_wins_conflicting_targeted_crop(self):
        selected,reason=select_voter_id('YWB3562154 Name: K SHRUTHI','YWB3962194',['YWB3562154','YWB3962194'])
        self.assertEqual((selected,reason),('YWB3562154','EXACT_CARD_HEADER'))

    def test_id_is_blank_when_absent_or_ambiguous(self):
        self.assertEqual(select_voter_id('no printed id','',['']), (None,'NOT_FOUND'))
        self.assertEqual(select_voter_id('ABC1234567 ABD1234567','',['ABC1234567','ABD1234567']), (None,'AMBIGUOUS'))

    def test_parse_concatenated_raw_text_field_labels(self):
        fields=parse_fields('Name : RAVI KUMARFathers Name: RAMUHouse Number : 1-23Age : 37 Gender : Male')
        self.assertEqual(fields['name'],'RAVI KUMAR')
        self.assertEqual(fields['relation_type'],'Father')
        self.assertEqual(fields['relation_name'],'RAMU')
        self.assertEqual(fields['house_no'],'1-23')
        self.assertEqual(fields['age'],37)
        self.assertEqual(fields['gender'],'Male')

    def test_parse_ocr_punctuation_substitutions_without_shifting_values(self):
        fields=parse_fields('Name ! HAREESH J | Fathers Name: GOPAL J House Number\ufffd: 1/826 Photo Age : 32 Gender : Male Available')
        self.assertEqual(fields['name'],'HAREESH J')
        self.assertEqual(fields['relation_name'],'GOPAL J')
        self.assertEqual(fields['house_no'],'1/826')
        self.assertEqual(fields['age'],32)
        self.assertEqual(fields['gender'],'Male')

    def test_parse_house_number_unicode_punctuation(self):
        fields=parse_fields('Name ! HAREESH J Fathers Name: GOPAL J House Number\u2019: 1/826 Photo Age : 32 Gender : Male')
        self.assertEqual(fields['house_no'],'1/826')

    def test_serial_can_be_read_from_leading_card_header(self):
        self.assertEqual(serial_header_candidate('| 33 YWB2612067\nName: A PERSON'),33)
        self.assertEqual(serial_header_candidate('YWB2612067\nName: A PERSON'),None)

    def test_empty_card_rejected(self):
        self.assertFalse(looks_like_card(Image.new('RGB',(250,160),'white')))

    def test_grid_yields_configured_slots(self):
        cfg={'grid':{'columns':3,'rows':2,'left_margin':0,'right_margin':0,'cards_top':0,'cards_bottom':0,'card_inner_padding_x':0,'card_inner_padding_y':0}}
        self.assertEqual(len(list(boxes(Image.new('RGB',(300,200)),cfg))),6)

    def test_reconciliation_flags_bad_rows_and_missing_serials(self):
        rows=[{'serial_no':1,'voter_id':'ABC1234567','name':'N','age':20,'gender':'Male'}, {'serial_no':1,'voter_id':'ABC1234567','name':'M','age':21,'gender':'Female'}]
        report,dups,missing,dup_ids,out_of_range=reconcile(rows,{'target_total':2,'target_male':1,'target_female':1,'target_third_gender':0,'target_start_serial':1,'target_end_serial':2})
        self.assertEqual(dups,['1']); self.assertEqual(missing,['2']); self.assertEqual(dup_ids,['ABC1234567'])
        self.assertEqual(out_of_range,[])
        self.assertTrue(any(r['status']=='REVIEW_REQUIRED' for r in report))

    def test_failed_target_is_explicit(self):
        report,*_=reconcile([],{})
        self.assertEqual(report[0]['status'],'TARGET_EXTRACTION_FAILED')

    def test_target_parser_uses_labeled_values_only(self):
        parsed=parse_target_text('Part Number 3 Year 2026 Starting Serial No 1 Ending Serial No 652 Male 330 Female 322 Third Gender 0 Total 652')
        self.assertEqual(parsed['target_total'],652)
        self.assertEqual(parsed['target_male'],330)
        self.assertEqual(parsed['target_end_serial'],652)
        self.assertIsNone(parse_target_text('Part Number 3 Year 2026 Total Pages 26')['target_total'])

    def test_serial_extraction_needs_printed_label(self):
        self.assertEqual(serial_candidates('Serial No: 42'),[42])
        self.assertEqual(serial_candidates('Page 3 row 4 column 2'),[])

    def test_gender_candidates_are_explicit_words_only(self):
        self.assertEqual(gender_candidates('Age: 30 Gender: Female'),['Female'])
        self.assertEqual(gender_candidates('Male and Female'),['Female','Male'])
        self.assertEqual(gender_candidates('Age: 30'),[])

    def test_reconciliation_flags_out_of_range_serials(self):
        rows=[{'serial_no':9,'voter_id':None,'name':'N','age':25,'gender':'Male'}]
        report,dups,missing,dup_ids,out_of_range=reconcile(rows,{'target_total':1,'target_start_serial':1,'target_end_serial':1})
        self.assertEqual(out_of_range,['9'])
        self.assertTrue(any(r['check']=='Out-of-range serial numbers' and r['status']=='REVIEW_REQUIRED' for r in report))
        self.assertTrue(any(r['check']=='Missing voter IDs' and r['actual']==1 for r in report))

    def test_table_text_is_not_a_voter_card(self):
        self.assertFalse(has_voter_card_evidence('Net Electors 652 Page 2',[]))
        self.assertTrue(has_voter_card_evidence('Name: A Father Name: B Age: 44',[]))

if __name__=='__main__': unittest.main()
