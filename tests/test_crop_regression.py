import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import cut_apartments as ca

class CropRegression(unittest.TestCase):
    def test_supported_pastel_is_retained(self):
        rgb=np.full((160,160,3),255,np.uint8)
        rgb[20:80,20:80]=[240,255,240]
        rgb[90:150,90:150]=[255,242,242]
        sat=ca.adaptive_saturation(rgb,0.06)
        fill=ca.colored_fill_mask(rgb,sat,0.45)
        self.assertTrue(fill[40,40])
        self.assertTrue(fill[120,120])
        self.assertFalse(fill[0,0])
    def test_coloured_annotation_does_not_lower_threshold(self):
        rgb=np.full((160,160,3),255,np.uint8)
        rgb[20,20]=[240,255,240]
        self.assertEqual(ca.adaptive_saturation(rgb,0.06),0.06)
    def test_balconies_compete_instead_of_merging_first(self):
        rooms=np.zeros((90,90),np.int32)
        rooms[10:35,40:80]=1
        rooms[50:75,40:80]=2
        rooms[10:35,5:25]=3
        rooms[50:75,5:25]=4
        rgb=np.full((90,90,3),255,np.uint8)
        rgb[rooms>0]=[255,255,236]
        a=ca.Apartment('A-4.2','4','2',(60,20))
        b=ca.Apartment('A-4.3','4','3',(60,60))
        edges=[(1,3,4),(3,1,3),(3,2,4)]
        for seeds in ({1:a,2:b},{2:b,1:a}):
            groups,_,_=ca.seeded_room_groups(rooms,4,edges,seeds,rgb)
            self.assertEqual(set(groups[1]),{1,3})
            self.assertEqual(set(groups[2]),{2,4})
    def test_other_tone_does_not_join_through_small_gap(self):
        rooms=np.zeros((40,70),np.int32)
        rooms[5:35,5:30]=1
        rooms[5:35,31:60]=2
        rgb=np.full((40,70,3),255,np.uint8)
        rgb[rooms==1]=[255,255,236]
        rgb[rooms==2]=[240,255,240]
        a=ca.Apartment('A-1.1','1','1',(15,20))
        groups,_,_=ca.seeded_room_groups(rooms,2,[(1,1,2)],{1:a},rgb)
        self.assertEqual(groups[1],[1])
    def test_duplicate_table_label_is_not_missing(self):
        rgb=np.full((100,160,3),255,np.uint8)
        rgb[20:80,10:70]=[240,255,240]
        labels=[ca.Apartment('A-1.1','1','1',(30,40)),ca.Apartment('A-1.1','1','1',(145,40))]
        a,missing,_,_=ca.raster_apartments(rgb,labels,ca.default_args(),1)
        self.assertEqual(len(a),1)
        self.assertEqual(missing,[])

    def test_furniture_does_not_change_room_colour(self):
        # Маленький санвузол намальовано щільніше за велику кімнату. Колір
        # кімнати — це колір її підлоги, а не середнє разом із меблями.
        rgb=np.full((60,120,3),255,np.uint8)
        rooms=np.zeros((60,120),np.int32)
        rooms[10:50,10:50]=1
        rooms[10:50,70:110]=2
        rgb[rooms>0]=[255,255,236]
        for y in range(14,46,3):                       # ванна, унітаз, мийка
            rgb[y,74:106]=[30,30,30]
        got=ca.room_fill_colours(rooms,2,rgb)
        self.assertLess(float(np.linalg.norm(got[1]-got[2])),1.0)

    def test_wall_costs_more_than_own_door(self):
        # Відтворює справжню помилку: сусідська спальня діставалась не тій
        # квартирі. Власні двері через ВЕЛИКУ кімнату дають довгий шлях, а
        # ланцюжок дрібних шаф спина до спини — короткий, хоч і перетинає
        # міжквартирну стіну. Якщо штраф за стіну множити на відстань, як було,
        # виграє ланцюжок; штраф має важити сам по собі.
        centres={1:20,2:201,3:346,4:394,5:542,7:659,6:780}
        rooms=np.zeros((820,60),np.int32)
        for room,y in centres.items():
            rooms[y-10:y+10,20:40]=room
        rgb=np.full((820,60,3),np.uint8(255))
        rgb[rooms>0]=[255,255,236]
        a=ca.Apartment('A-4.8','4','8',(30,centres[1]))
        b=ca.Apartment('A-4.9','4','9',(30,centres[6]))
        edges=[(11.0,1,2),(2.0,2,3),(19.0,3,4),(2.0,4,5),(2.0,5,7),(2.0,7,6)]
        for seeds in ({1:a,6:b},{6:b,1:a}):
            groups,_,_=ca.seeded_room_groups(rooms,7,edges,seeds,rgb,unit=13.9)
            self.assertEqual(set(groups[1]),{1,2,3})
            self.assertEqual(set(groups[6]),{4,5,6,7})

    def test_shared_wall_shown_whole_without_shared_interior(self):
        # Спільну стіну дозволено показати в обох PNG, але зафарбовані
        # підлоги двох квартир перетинатись не мають.
        rgb=np.full((60,160,3),255,np.uint8)
        rgb[10:50,10:70]=[255,255,236]
        rgb[10:50,90:150]=[255,255,236]
        rgb[10:50,70:90]=[40,40,40]                    # стіна між ними
        left=np.zeros((60,160),bool); left[10:50,10:70]=True
        right=np.zeros((60,160),bool); right[10:50,90:150]=True
        out=ca.refine_room_masks([left,right],rgb,1.0,5.0)
        wall=(slice(20,40),slice(74,86))
        self.assertTrue(out[0][wall].any())
        self.assertTrue(out[1][wall].any())
        floor=ca.colored_fill_mask(rgb,ca.adaptive_saturation(rgb,0.06),0.45)
        self.assertFalse(((out[0]&floor)&(out[1]&floor)).any())

    def test_area_column_is_not_reported_missing(self):
        # У відомості колонка площ виглядає так само, як номер квартири
        # («26.78», підсумок «905.14»). Такі рядки не є втраченими квартирами.
        cut=[ca.Apartment('A-24.1','24','1',(0,0)),
             ca.Apartment('A-24.2','24','2',(0,0))]
        seen=list(cut)+[ca.Apartment('A-26.78','26','78',(0,0)),
                        ca.Apartment('A-905.14','905','14',(0,0)),
                        ca.Apartment('A-24.3','24','3',(0,0))]
        self.assertEqual(ca.unmatched_labels(seen,cut),['A-24.3'])

    def test_missing_reported_when_nothing_was_cut(self):
        seen=[ca.Apartment('A-24.1','24','1',(0,0))]
        self.assertEqual(ca.unmatched_labels(seen,[]),['A-24.1'])

if __name__=='__main__':unittest.main()
