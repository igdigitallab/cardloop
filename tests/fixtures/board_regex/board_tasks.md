# Tasks - synthetic fixture for the board regex characterization test

Preamble bullet that must stay out of the cards.
- a preamble bullet <!--ops:pre0-->

## Backlog
- [ ] plain checkbox card <!--ops:aaaa1111-->
- [ ] card with meta <!--ops:bbbb2222 model=haiku spec=096 rt=1790000000-->
- [ ] provider card <!--ops:cccc3333 provider=codex model=gpt-5.5 rt=1790000001-->
- [ ] marker mid text   <!--ops:dddd4444-->   and text after it
- [ ] two markers <!--ops:eeee5555--> between <!--ops:ffff6666 spec=1--> end
- [ ] marker at the very start of the text:
- [ ]   <!--ops:gggg7777-->   leading marker only
- [ ] marker spaced inside <!-- ops:hhhh8888 --> comment
- [ ] marker no space <!--ops:iiii9999-->tail
- [ ] id with dashes <!--ops:jan-9e2d-->
- [ ] id that is only dashes <!--ops:-->
- [ ] dash id then meta <!--ops:- rt=1790000002-->
- [ ] bad meta <!--ops:kkkk0000 model=bad/slash spec=UP rt=12-->
- [ ] unterminated <!--ops:llll1111 model=haiku
- [ ] not a marker <!-- ops-ish --> still text
- [ ] marker with gt inside <!--ops:mmmm2222 a>b-->
- [ ] no marker at all
-    [ ] extra spaces before the checkbox <!--ops:nnnn3333-->
-[ ]no spaces at all <!--ops:oooo4444-->
*[x]star cards <!--ops:pppp5555-->
* [x] star card done <!--ops:qqqq6666-->
	- [ ] tab indented card <!--ops:rrrr7777-->
  	 * [?]	tab after the checkbox	<!--ops:ssss8888-->	
- [ ] trailing spaces <!--ops:tttt9999-->      
- [ ]
- [ ]    
- [x]		
* [a] odd status char
- [ ] nbsp  <!--ops:uuuu0000-->  after 
- [ ] em space <!--ops:vvvv1111--> end
- [ ] ideographic　<!--ops:wwww2222-->
- plain bullet card
- plain bullet with marker <!--ops:xxxx3333 model=sonnet-->
* star plain card <!--ops:yyyy4444-->
-  [not a checkbox but two spaces
- [not a checkbox, one space
-   
-    
- 
-
*  	 
* plain   <!--ops:zzzz5555-->    
- text with <!--ops:aaaa0001--> <!--ops:aaaa0002--> <!--ops:aaaa0003-->
  > description line one
  > description line two <!--ops:notacard-->
  >not a description (one space short)

## In Progress
- [~] running card <!--ops:run11111 model=opus-->
  > a description
- running plain <!--ops:run22222-->

## Review
- [?] in review <!--ops:rev11111 rt=1790000003-->

## Failed
- [!] failed card <!--ops:fai11111-->

## Notes
- [ ] a card under an unknown section is dropped <!--ops:unk11111-->
